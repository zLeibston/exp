"""Native HTTP adapters with shared transport retries and token-budget handling."""
import time
import logging
import math
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from harness.meta.agent.protocol import encode_messages, tool_options, decode_reply


class APIError(RuntimeError):
    pass


class EmptyResponse(APIError):
    pass


class BudgetExceeded(APIError):
    pass


def retry_delay(attempt, response=None):
    value = response.headers.get('Retry-After') if response is not None else None
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                delay = ((date if date.tzinfo else date.replace(tzinfo=timezone.utc))
                         - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                delay = float('nan')
        if math.isfinite(delay):
            return max(0.0, delay)
    # Jitter prevents all workers from retrying a rate-limited endpoint together.
    return min(2 ** attempt, 8) * random.uniform(0.75, 1.25)


def pause_retry(protocol, attempt, reason, response=None):
    delay = retry_delay(attempt, response)
    logging.getLogger(__name__).warning('%s: API retry %s (%s), delay=%.2fs',
                                       protocol, attempt + 1, reason, delay)
    time.sleep(delay)


def terminal_empty_reason(protocol, data):
    if protocol == 'openai':
        choices = data.get('choices') or [{}]
        return choices[0].get('finish_reason')
    if protocol == 'anthropic':
        return data.get('stop_reason')
    candidates = data.get('candidates') or [{}]
    return candidates[0].get('finishReason') or data.get('promptFeedback', {}).get('blockReason')



def endpoint(base, suffix):
    base = base.rstrip("/")
    return base if base.endswith(suffix) else base + suffix


def openai_request(config, messages, tools=None):
    name = config.model_name
    base = config.url or ("https://api.deepseek.com" if name.startswith("deepseek") else "https://api.openai.com/v1")
    if base.rstrip("/") == "https://api.openai.com":
        base = base.rstrip("/") + "/v1"
    body = dict(model=name, messages=messages, stream=False)
    body.update(tool_options("openai", tools))
    if config.is_deepseek and config.json_mode and not tools:
        body["response_format"] = {"type": "json_object"}
    if config.reasoning_effort is not None:
        body['reasoning_effort'] = config.reasoning_effort
        if config.is_deepseek:
            body['thinking'] = {'type': 'disabled' if config.reasoning_effort == 'none' else 'enabled'}
    reasoning = name.startswith(("gpt-5", "o1", "o3", "o4"))
    body["max_completion_tokens" if reasoning else "max_tokens"] = config.max_tokens
    if config.temperature is not None:
        body["temperature"] = config.temperature
    return endpoint(base, "/chat/completions"), {"Authorization": f"Bearer {config.api_key}"}, body


def anthropic_request(config, messages, tools=None):
    body = dict(model=config.model_name, messages=messages, max_tokens=config.max_tokens)
    body.update(tool_options("anthropic", tools))
    if config.temperature is not None:
        body["temperature"] = config.temperature
    base = config.url or "https://api.anthropic.com/v1"
    if base.rstrip("/").endswith("api.anthropic.com"):
        base = base.rstrip("/") + "/v1"
    return endpoint(base, "/messages"), {"x-api-key": config.api_key, "anthropic-version": "2023-06-01"}, body


def gemini_request(config, messages, tools=None):
    base = config.url or "https://generativelanguage.googleapis.com/v1beta"
    if base.rstrip("/").endswith("generativelanguage.googleapis.com"):
        base = base.rstrip("/") + "/v1beta"
    name = quote(config.model_name.removeprefix("models/"), safe="-._")
    url = base if base.endswith(":generateContent") else base.rstrip("/") + f"/models/{name}:generateContent"
    generation = {"maxOutputTokens": config.max_tokens}
    if config.json_mode and not tools:
        generation["responseMimeType"] = "application/json"
    if config.temperature is not None:
        generation["temperature"] = config.temperature
    body = dict(contents=messages, generationConfig=generation)
    body.update(tool_options("gemini", tools))
    return url, {"x-goog-api-key": config.api_key}, body


BUILDERS = {"openai": openai_request, "anthropic": anthropic_request, "gemini": gemini_request}


def extract_reply(protocol, data):
    try:
        reason = str(terminal_empty_reason(protocol, data) or '').lower()
        if reason in {'length', 'max_tokens'}:
            raise BudgetExceeded(f'{protocol}: output token budget exhausted; partial output discarded')
        if reason in {'aborted', 'insufficient_system_resource'}:
            raise EmptyResponse(f'{protocol}: response interrupted ({reason})')
        if reason in {'content_filter', 'safety', 'refusal', 'recitation', 'blocklist', 'prohibited_content', 'spii'}:
            raise APIError(f'{protocol}: response blocked ({reason})')
        reply = decode_reply(protocol, data)
        if not reply['text'].strip() and not reply['tool_calls']:
            raise EmptyResponse(f'{protocol}: empty response text and tool calls')
        return reply
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise APIError(f"{protocol}: no usable response (blocked, truncated, or incompatible protocol)") from None


def complete(config, messages, tools=None, *, structured=False):
    import httpx
    protocol = config.protocol
    if not structured:
        messages = [dict(role=m['role'], text=m['content']) for m in messages]
    url, headers, body = BUILDERS[protocol](config, encode_messages(protocol, messages), tools)
    headers["Content-Type"] = "application/json"
    # Share the existing retry allowance across both reasoning modes. With the
    # defaults, at most 4 HTTP attempts: initial + 2 transient retries + 1 downgrade.
    retries_used = 0
    downgraded = False

    def retry(reason, response=None):
        nonlocal retries_used
        if retries_used >= config.retries:
            return False
        pause_retry(protocol, retries_used, reason, response)
        retries_used += 1
        return True

    # Do not log request headers, raw response errors, or exception URLs.
    with httpx.Client(timeout=config.timeout, follow_redirects=False) as client:
        while True:
            try:
                response = client.post(url, headers=headers, json=body)
            except httpx.TransportError:
                if retry('transport error'):
                    continue
                raise APIError(f"{protocol}: connection failed or timed out") from None
            if response.status_code in {408, 429} or 500 <= response.status_code < 600:
                if retry(f'HTTP {response.status_code}', response):
                    continue
            if response.status_code >= 400:
                raise APIError(f"{protocol}: HTTP {response.status_code}; check model, URL, credentials and quota")
            try:
                data = response.json()
            except ValueError:
                raise APIError(f"{protocol}: endpoint returned invalid JSON") from None
            try:
                reply = extract_reply(protocol, data)
                return reply if structured else reply['text']
            except BudgetExceeded:
                if (protocol == 'openai' and config.is_deepseek and config.budget_fallback and not downgraded
                        and body.get('reasoning_effort') != 'none'):
                    body = {**body, 'reasoning_effort': 'none', 'thinking': {'type': 'disabled'}}
                    downgraded = True
                    logging.getLogger(__name__).warning(
                        'deepseek: output budget exhausted; retrying once with thinking disabled')
                    continue
                raise
            except EmptyResponse as exc:
                if retry(str(exc)):
                    continue
                if (protocol == 'openai' and config.is_deepseek and config.budget_fallback and not downgraded
                        and body.get('reasoning_effort') not in {None, 'none'}):
                    body = {**body, 'reasoning_effort': 'none', 'thinking': {'type': 'disabled'}}
                    downgraded = True
                    logging.getLogger(__name__).warning(
                        'deepseek: empty responses after retries; trying once with thinking disabled')
                    continue
                raise


def extract_text(protocol, data):
    return extract_reply(protocol, data)['text']

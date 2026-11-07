"""New process per call: load the selected snapshot, never stale imported code."""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from itertools import count
from pathlib import Path


def is_context_limit(error):
    """Recognize explicit context limits without retaining provider error messages."""
    codes = {'context_length_exceeded', 'context_window_exceeded', 'prompt_too_long'}
    message = str(error.get('message') or '').lower()
    return (error.get('code') in codes or error.get('type') in codes
            or message.startswith(('prompt is too long', 'input is too long'))
            or ('input token count' in message and 'exceeds the maximum' in message))


def install_api_diagnostics(path, role, model):
    """Observe HTTP attempts without changing payloads, retries or response handling."""
    import httpx
    original_send = httpx.Client.send
    calls = count(1)
    state = dict(failed=False, fatal=False, context_limit=False)

    def save(record):
        module = sys.modules.get('harness.task.agent.trace')
        trace = module.ACTIVE.get() if module else None
        if trace is not None:
            # Reuse HTTP instrumentation; expose numeric/status metadata only.
            keys = ('call_id', 'event', 'elapsed_seconds', 'http_status', 'usage',
                    'finish_reason', 'error_type', 'request_reasoning_effort',
                    'request_max_tokens', 'request_max_completion_tokens', 'request_thinking', 'context_limit')
            fields = {key: record[key] for key in keys if key in record}
            fields['http_event'] = fields.pop('event')
            fields['attempt_id'] = fields.pop('call_id')
            if 'elapsed_seconds' in fields:
                fields['duration_seconds'] = fields.pop('elapsed_seconds')
            trace.event('api_attempt', **fields)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            # Diagnostics must never turn a successful inference into a failure.
            print(f"API diagnostics could not be saved: {type(exc).__name__}", file=sys.stderr, flush=True)

    def length(value):
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(length(part.get("text")) for part in value if isinstance(part, dict))
        return 0

    def nonwhitespace_length(value):
        if isinstance(value, str):
            return sum(not char.isspace() for char in value)
        if isinstance(value, list):
            return sum(nonwhitespace_length(part.get("text")) for part in value if isinstance(part, dict))
        return 0

    def numeric_usage(value):
        if not isinstance(value, dict):
            return {}
        return {key: numeric_usage(item) if isinstance(item, dict) else item
                for key, item in value.items() if isinstance(item, (int, float, dict))}

    def send(client, request, *args, **kwargs):
        state['failed'] = False
        state['context_limit'] = False
        record = dict(call_id=next(calls), role=role, requested_model=model,
                      started_at=datetime.now(timezone.utc).isoformat())
        try:
            payload = json.loads(request.content)
            if isinstance(payload, dict):
                for key in ("model", "max_tokens", "max_completion_tokens", "response_format", "reasoning_effort", "thinking", "generationConfig"):
                    if key in payload:
                        record["request_" + key] = payload[key]
        except Exception:
            pass
        save(dict(record, event="request_started"))
        started = time.monotonic()
        try:
            response = original_send(client, request, *args, **kwargs)
        except Exception as exc:
            state['failed'] = True
            save(dict(record, event="transport_error", elapsed_seconds=round(time.monotonic() - started, 3),
                      error_type=type(exc).__name__))
            raise
        state['failed'] = response.status_code >= 400
        state['fatal'] |= response.status_code == 402
        record.update(event="response", elapsed_seconds=round(time.monotonic() - started, 3),
                      http_status=response.status_code,
                      request_id=response.headers.get("x-request-id") or response.headers.get("request-id")
                                 or response.headers.get("x-goog-request-id"))
        try:
            # Never consume a stream or mutate the response to collect diagnostics.
            try:
                data = response.json()
            except ValueError:
                state['failed'] = True
                raise
            record["response_type"] = type(data).__name__
            if isinstance(data, dict):
                record.update(response_fields=sorted(data), returned_model=data.get("model", data.get("modelVersion")),
                              usage=numeric_usage(data.get("usage", data.get("usageMetadata", {}))))
                choices = data.get("choices", data.get("candidates", []))
                record["choices_count"] = len(choices) if isinstance(choices, list) else None
                first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
                message = first.get("message", {})
                message = message if isinstance(message, dict) else {}
                content = message.get("content") if "choices" in data else data.get("content")
                parts = first.get("content", {}).get("parts", []) if isinstance(first.get("content"), dict) else []
                if "candidates" in data:
                    content = [part for part in parts if isinstance(part, dict) and not part.get("thought", False)]
                # Count native calls in the same first response used for text diagnostics.
                if "choices" in data:
                    tool_calls = message.get("tool_calls")
                    record["tool_call_count"] = len(tool_calls) if isinstance(tool_calls, list) else 0
                elif "candidates" in data:
                    record["tool_call_count"] = sum(isinstance(part, dict) and "functionCall" in part for part in parts)
                else:
                    record["tool_call_count"] = sum(isinstance(part, dict) and part.get("type") == "tool_use"
                                                    for part in content) if isinstance(content, list) else 0
                record.update(finish_reason=first.get("finish_reason", first.get("finishReason", data.get("stop_reason"))),
                              message_fields=sorted(message), content_type=type(content).__name__,
                              content_length=length(content), content_nonwhitespace_length=nonwhitespace_length(content))
                record["reasoning_lengths"] = {key: length(message[key]) for key in
                                               ("reasoning_content", "reasoning", "thinking", "analysis") if key in message}
                if isinstance(content, list):
                    record["content_block_types"] = [part.get("type") for part in content if isinstance(part, dict)]
                    record["thinking_length"] = sum(length(part.get("thinking")) for part in content if isinstance(part, dict))
                if "candidates" in data:
                    record.update(content_type=type(parts).__name__,
                                  content_length=sum(length(part.get("text")) for part in parts if isinstance(part, dict) and not part.get("thought")),
                                  thinking_length=sum(length(part.get("text")) for part in parts if isinstance(part, dict) and part.get("thought")))
                if isinstance(data.get("error"), dict):
                    record["error_type"] = data["error"].get("type")
                    record["error_code"] = data["error"].get("code")
                    record['context_limit'] = (response.status_code in {400, 413, 422}
                                               and is_context_limit(data['error']))
                    state['context_limit'] = record['context_limit']
                    state['failed'] = state['failed'] and not state['context_limit']
        except Exception as exc:
            record["response_diagnostic_error"] = type(exc).__name__
        save(record)
        if response.status_code == 402:
            # Fixed controller signal, including for older archived harnesses.
            # SystemExit bypasses ordinary agent/tool exception recovery.
            print("API HTTP 402: insufficient balance; stopping run", file=sys.stderr, flush=True)
            raise SystemExit(78)
        return response

    httpx.Client.send = send
    return state


def unresolved_api_failure(path):
    """Ignore in-flight requests; only explicit failures without later recovery count."""
    failed = False
    if Path(path).is_file():
        with Path(path).open(errors='replace') as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # A killed worker can leave an incomplete final line.
                if not isinstance(record, dict):
                    continue
                if record.get('event') == 'transport_error':
                    failed = True
                elif record.get('event') == 'response' and type(record.get('http_status')) is int:
                    failed = record['http_status'] >= 400 and not record.get('context_limit', False)
    return failed


def failed_task_result(directory, reason, error_type=None):
    """Recover the bounded message journal so failed trials remain Meta evidence."""
    history = []
    journal = Path(directory) / 'history.partial.jsonl'
    if journal.is_file():
        with journal.open('rb') as handle:
            for line in handle:
                try:
                    history.append(json.loads(line)['message'])
                except (ValueError, KeyError):
                    break  # A killed process may leave one incomplete final line.
    return dict(prediction=None, history=history,
                task_failure=dict(reason=reason, error_type=error_type))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    workspace = Path(request["workspace"])
    from api_config import load_config
    sys.path.insert(0, request.get("runtime_workspace", str(workspace)))
    role = "task_agent" if request["kind"] == "task" else "meta_agent"
    os.environ["RSI_CONFIG_PATH"] = request["model"]["config_path"]
    os.environ["RSI_AGENT_ROLE"] = role
    model = load_config(request["model"]["config_path"], roles=(role,))[role].model
    api_state = install_api_diagnostics(Path(request["log_path"]).parent / "api_calls.jsonl",
                                        os.environ.get("RSI_AGENT_ROLE"), model)
    kwargs = dict(model=model, chat_history_file=request["log_path"])
    if request["kind"] == "task":
        import httpx
        try:
            from harness.task.task_agent import TaskAgent
            prediction, history = TaskAgent(**kwargs).forward(request["inputs"])
            result = dict(prediction=prediction, history=history)
        except (Exception, SystemExit) as exc:
            # Classify from fixed HTTP observations, never from evolvable class names.
            if api_state['fatal']:
                raise SystemExit(78)
            if (api_state['failed'] or isinstance(exc, httpx.HTTPError)) and not api_state.get('context_limit'):
                raise
            traceback.print_exc()  # Preserve the failing code location in stderr.log.
            result = failed_task_result(Path(request['log_path']).parent,
                                        'context_limit' if api_state.get('context_limit') else 'agent_error',
                                        type(exc).__name__)
    else:
        if request['stage'] not in {'stability_repair', 'forgetting_repair'}:
            raise ValueError(f"Unknown Meta repair stage: {request['stage']}")
        from harness.meta.improve_agent import ImproveAgent
        history = ImproveAgent(**kwargs).forward(
            repo_path=Path(request.get('candidate_path', workspace / 'harness/task')),
            eval_path=request["history_path"],
            context_paths=request["context_paths"],
            instruction=request["instruction"],
            stage=request["stage"],
            iterations_left=request.get("iterations_left"))
        result = dict(history=history)
    result_path = Path(request["result_path"])
    if api_state['fatal']:
        raise SystemExit(78)
    temporary = result_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2))
    temporary.replace(result_path)
    if request['kind'] == 'task':
        (result_path.parent/'history.partial.jsonl').unlink(missing_ok=True)


if __name__ == "__main__":
    main()

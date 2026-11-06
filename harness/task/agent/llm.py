"""Role-configured model calls; native payloads stay inside harness history."""
import os
import time
from harness.task.agent.trace import ACTIVE

from dataclasses import replace
from harness.task.agent.config import load_config

OPENAI_MODEL = "openai/gpt-4o"
CLAUDE_MODEL = "anthropic/claude-sonnet-4-5-20250929"


def get_response_from_llm(msg, model=OPENAI_MODEL, temperature=None,
                          max_tokens=None, msg_history=None, tools=None):
    history = [dict(item) for item in (msg_history or [])]
    if msg is not None:
        history.append({"role": "user", "text": msg})
    path = os.environ.get("RSI_CONFIG_PATH")
    role = os.environ.get("RSI_AGENT_ROLE")
    if not path or role not in {"task_agent"}:
        raise ValueError("task harness requires a YAML config and its own API profile")
    config = load_config(path, roles=(role,))[role]
    if max_tokens is not None:
        config = replace(config, max_tokens=max_tokens)
    if temperature is not None:
        config = replace(config, temperature=temperature)
    from harness.task.agent.providers import complete
    trace = ACTIVE.get()
    started = time.monotonic()
    if trace:
        trace.iteration += 1
        trace.history(history)
        trace.summary()
        trace.event('model_start', input_messages=len(history),
                    input_text_chars=sum(len(str(item.get('text', ''))) for item in history))
    try:
        reply = complete(config, history, tools=tools, structured=True)
    except BaseException as exc:
        if trace:
            trace.event('model_error', error_type=type(exc).__name__, duration_seconds=round(time.monotonic()-started, 4))
        raise
    history.append(reply)
    if trace:
        trace.history(history)
        trace.event('model_end', message_index=len(history)-1, duration_seconds=round(time.monotonic()-started, 4))
    return reply['text'], history, {'tool_calls': reply['tool_calls']}

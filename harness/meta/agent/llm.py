"""Role-configured model calls; native payloads stay inside harness history."""
import os

from dataclasses import replace
from harness.meta.agent.config import load_config

OPENAI_MODEL = "openai/gpt-4o"
CLAUDE_MODEL = "anthropic/claude-sonnet-4-5-20250929"


def get_response_from_llm(msg, model=OPENAI_MODEL, temperature=None,
                          max_tokens=None, msg_history=None, tools=None):
    history = [dict(item) for item in (msg_history or [])]
    if msg is not None:
        history.append({"role": "user", "text": msg})
    path = os.environ.get("RSI_CONFIG_PATH")
    role = os.environ.get("RSI_AGENT_ROLE")
    if not path or role not in {"meta_agent"}:
        raise ValueError("meta harness requires a YAML config and its own API profile")
    config = load_config(path, roles=(role,))[role]
    if max_tokens is not None:
        config = replace(config, max_tokens=max_tokens)
    if temperature is not None:
        config = replace(config, temperature=temperature)
    from harness.meta.agent.providers import complete
    reply = complete(config, history, tools=tools, structured=True)
    history.append(reply)
    return reply['text'], history, {'tool_calls': reply['tool_calls']}

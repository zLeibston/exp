"""One tool loop shared by all providers; only final answers use text JSON."""
import json
from pathlib import Path

MAX_MODEL_CALLS = 80

from harness.meta.agent.llm import get_response_from_llm
from harness.meta.agent.tools import load_tools
from harness.meta.agent.protocol import tool_arguments
from harness.meta.utils.common import parse_json
from harness.meta.prompt import response_protocol_prompt, turn_instruction


def process_tool_call(tools_dict, tool_name, tool_input):
    try:
        if tool_name in tools_dict:
            return tools_dict[tool_name]['function'](**tool_input)
        return f"Error: Tool '{tool_name}' not found"
    except Exception as exc:
        return f"Error executing tool '{tool_name}': {exc}"


def chat_with_agent(msg, model="claude-4-sonnet-genai", msg_history=None, logging=print,
                    tools_available=None, max_tool_calls=40, finish_on_budget=False, state_path=None):
    history = msg_history if msg_history is not None else []
    tools = load_tools(logging=logging, names=tools_available or [])
    tools_dict = {tool['info']['name']: tool for tool in tools}
    schemas = [tool['info'] for tool in tools]
    prompt = response_protocol_prompt(schemas)
    model_calls_used = 0
    max_model_calls = MAX_MODEL_CALLS
    def reminder():
        remaining = max(0, max_tool_calls - num_tool_calls) if max_tool_calls > 0 else None
        return turn_instruction(remaining, max_model_calls - model_calls_used)

    next_message = msg + "\n\n" + prompt
    num_tool_calls = 0
    format_failures = 0
    if tools:
        next_message += "\n" + reminder()
    stop_reason = None
    error_type = None

    def persist(status):
        if state_path is None:
            return
        path = Path(state_path)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(dict(status=status, stop_reason=stop_reason, error_type=error_type,
            model_calls=model_calls_used, tool_calls=num_tool_calls,
            pending_message=next_message if status != 'completed' else None,
            history=history), ensure_ascii=False, indent=2))
        temporary.replace(path)

    logging(f"Input: {msg!r}")
    try:
        for _ in range(max_model_calls):
            tool_budget_exhausted = max_tool_calls > 0 and num_tool_calls >= max_tool_calls
            final_only = tool_budget_exhausted or model_calls_used == max_model_calls - 1
            if final_only:
                stop_reason = 'tool_budget' if tool_budget_exhausted else 'model_budget'
            persist('running')
            try:
                response, history, metadata = get_response_from_llm(
                    msg=next_message, model=model, msg_history=history,
                    tools=[] if final_only else schemas)
                model_calls_used += 1
            except Exception as exc:
                logging(f"Error: {exc}")
                raise
            persist('running')
            logging(f"Output: {response!r}")
            calls = metadata.get('tool_calls', [])
            if calls:
                # A provider may return several calls even when parallel calls are disabled.
                # Execute sequentially and return a result for every call, preserving IDs.
                format_failures = 0
                exhausted = False
                for call in calls:
                    is_error = False
                    if final_only or (max_tool_calls > 0 and num_tool_calls >= max_tool_calls):
                        output = 'Error: call budget exhausted; this call was not executed.'
                        is_error = exhausted = True
                    else:
                        num_tool_calls += 1
                        try:
                            arguments = tool_arguments(call)
                            output = process_tool_call(tools_dict, call['name'], arguments)
                            is_error = isinstance(output, str) and output.startswith('Error')
                        except (ValueError, TypeError) as exc:
                            output = f'Error: invalid tool arguments: {exc}'
                            is_error = True
                    result = dict(role='tool', call_id=call['id'], name=call['name'],
                                  text=str(output), is_error=is_error)
                    history.append(result)
                    persist('running')
                    logging(f"Tool call: {call!r}; result: {result!r}")
                if exhausted:
                    if finish_on_budget and final_only:
                        raise RuntimeError('Meta requested tools during final-only response')
                    if not finish_on_budget:
                        raise RuntimeError('Maximum number of tool calls reached')
                next_message = reminder()
                continue
            try:
                value = parse_json(response)
                if not isinstance(value.get('response'), str) or 'tool_name' in value or 'tool_input' in value:
                    raise ValueError('Final JSON must contain a response string; actions require native tool calls')
                stop_reason = stop_reason or 'final_response'
                persist('completed')
                return history
            except (ValueError, TypeError) as exc:
                format_failures += 1
                if format_failures >= 3:
                    raise RuntimeError("Three consecutive invalid model responses; inspect chat.log") from exc
                next_message = json.dumps(dict(error=f"Invalid output format: {exc}",
                                               instruction=prompt + '\n' + reminder()),
                                          ensure_ascii=False)
                logging(f"Format feedback: {next_message!r}")
        raise RuntimeError('Model response limit exceeded')
    except Exception as exc:
        stop_reason = stop_reason or 'error'
        error_type = type(exc).__name__
        persist('failed')
        raise

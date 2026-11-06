"""One tool loop shared by all providers; only final answers use text JSON."""
import json
import time
from harness.task.agent.trace import ACTIVE

from harness.task.agent.llm import get_response_from_llm
from harness.task.agent.tools import load_tools
from harness.task.agent.protocol import tool_arguments
from harness.task.utils.common import parse_json
from harness.task.prompt import response_protocol_prompt, FORMAT_RETRY_INSTRUCTION, turn_instruction


def process_tool_call(tools_dict, tool_name, tool_input):
    try:
        if tool_name in tools_dict:
            return tools_dict[tool_name]['function'](**tool_input)
        return f"Error: Tool '{tool_name}' not found"
    except Exception as exc:
        return f"Error executing tool '{tool_name}': {exc}"


def chat_with_agent(msg, model="claude-4-sonnet-genai", msg_history=None, logging=print,
                    tools_available=None, max_tool_calls=40, finish_on_budget=False):
    history = msg_history if msg_history is not None else []
    tools = load_tools(logging=logging, names=tools_available or [])
    tools_dict = {tool['info']['name']: tool for tool in tools}
    schemas = [tool['info'] for tool in tools]
    prompt = response_protocol_prompt(schemas)
    def reminder():
        remaining = max(0, max_tool_calls - num_tool_calls) if max_tool_calls > 0 else None
        return turn_instruction(remaining) if tools else prompt

    trace = ACTIVE.get()
    next_message = prompt + "\n\n" + msg
    num_tool_calls = 0
    format_failures = 0
    if tools:
        next_message += "\n" + reminder()
    logging(f"Input: {msg!r}")
    for _ in range(80):
        try:
            response, history, metadata = get_response_from_llm(
                msg=next_message, model=model, msg_history=history, tools=schemas)
        except Exception as exc:
            logging(f"Error: {exc}")
            raise
        logging(f"Output: {response!r}")
        calls = metadata.get('tool_calls', [])
        if calls:
            # A provider may return several calls even when parallel calls are disabled.
            # Execute sequentially and return a result for every call, preserving IDs.
            format_failures = 0
            exhausted = False
            assistant_message_index = len(history)-1
            for call in calls:
                started = time.monotonic()
                executed = False
                if trace:
                    trace.tool_fields = {}
                    trace.event('tool_start', call_id=call.get('id'), tool=call['name'], assistant_message_index=assistant_message_index)
                is_error = False
                if max_tool_calls > 0 and num_tool_calls >= max_tool_calls:
                    output = 'Error: tool budget exhausted; this call was not executed.'
                    is_error = exhausted = True
                else:
                    num_tool_calls += 1
                    try:
                        arguments = tool_arguments(call)
                        executed = call['name'] in tools_dict
                        output = process_tool_call(tools_dict, call['name'], arguments)
                        is_error = isinstance(output, str) and output.startswith('Error')
                    except (ValueError, TypeError) as exc:
                        output = f'Error: invalid tool arguments: {exc}'
                        is_error = True
                result = dict(role='tool', call_id=call['id'], name=call['name'],
                              text=str(output), is_error=is_error)
                history.append(result)
                if trace:
                    trace.history(history)
                    trace.event('tool_end', call_id=call.get('id'), tool=call['name'],
                        result_message_index=len(history)-1, dispatched=executed, is_error=is_error,
                        returned_chars=len(str(output)), duration_seconds=round(time.monotonic()-started, 4),
                        **trace.tool_fields)
                logging(f"Tool call: {call!r}; result: {result!r}")
            if exhausted:
                if trace: trace.reason = 'tool_budget'
                if finish_on_budget:
                    logging('Stop: tool budget exhausted; existing edits will be checked by the controller.')
                    return history
                raise RuntimeError('Maximum number of tool calls reached')
            next_message = reminder()
            continue
        try:
            value = parse_json(response)
            if 'response' not in value or 'tool_name' in value or 'tool_input' in value:
                raise ValueError('Final JSON must contain response; actions require native tool calls')
            if trace: trace.reason = 'final_response'
            return history
        except (ValueError, TypeError) as exc:
            format_failures += 1
            if trace: trace.event('format_rejected', consecutive_failures=format_failures)
            if format_failures >= 3:
                if trace: trace.reason = 'format_errors'
                raise RuntimeError("Three consecutive invalid model responses; inspect chat.log") from exc
            next_message = json.dumps(dict(error=f"Invalid output format: {exc}",
                                           instruction=FORMAT_RETRY_INSTRUCTION + "\n" + prompt),
                                      ensure_ascii=False)
            logging(f"Format feedback: {next_message!r}")
    if trace: trace.reason = 'model_budget'
    raise RuntimeError('Model response limit exceeded')

"""Task instructions and response protocol; owned by the task harness."""
import json

TASK_INSTRUCTION = 'You are an agent aimed at solving tasks. '

FORMAT_RETRY_INSTRUCTION = 'Use the native tool interface for actions, or finish with one JSON object containing response.'


def task_instruction(inputs):
    instruction = (TASK_INSTRUCTION + "\n\nTask input:\n```json\n"
                   + json.dumps(inputs, ensure_ascii=False)
                   + '\n```')
    if inputs.get("response_labels"):
        instruction += '\nThe response field must be a string equal to one of: ' + json.dumps(inputs["response_labels"])
    return instruction


def response_protocol_prompt(tool_infos=None):
    prompt = 'For your final answer, return exactly one JSON object: {"response": "your answer"}, without Markdown or surrounding text.'
    if tool_infos:
        prompt += ' Use the provided native tools for actions; do not write tool calls as response text.'
    return prompt


def turn_instruction(remaining):
    text = 'Use native tools to continue, or finish with {"response": "your answer"}.'
    if remaining is not None:
        text += f" Tool calls remaining: {remaining}."
        if remaining <= 10:
            text += " Finish the concrete change and check it now; avoid further broad exploration."
    return text

"""Native wire formats behind one history/tool-call representation.

Opaque assistant payloads are replayed unchanged (including thinking signatures).
The loop only uses text and calls; it never interprets provider-specific blocks.
"""

def encode_messages(protocol, history):
    messages = []
    for item in history:
        role = item['role']
        if item.get('native_protocol') == protocol:
            messages.append(item['native'])
            continue
        if role == 'tool':
            if protocol == 'openai':
                message = dict(role='tool', tool_call_id=item['call_id'], content=item['text'])
            elif protocol == 'anthropic':
                message = dict(role='user', content=[dict(type='tool_result', tool_use_id=item['call_id'],
                                                         content=item['text'], is_error=item.get('is_error', False))])
            else:
                result = dict(name=item['name'], response={'output': item['text']})
                if item.get('call_id'):
                    result['id'] = item['call_id']
                message = dict(role='user', parts=[{'functionResponse': result}])
        elif protocol == 'gemini':
            message = dict(role='model' if role == 'assistant' else role, parts=[{'text': item['text']}])
        elif protocol == 'anthropic':
            message = dict(role=role, content=[{'type': 'text', 'text': item['text']}])
        else:
            message = dict(role=role, content=item['text'])
        # All results from a parallel call batch must precede any new user text.
        if protocol != 'openai' and messages and role in {'tool', 'user'} and messages[-1]['role'] == 'user':
            key = 'parts' if protocol == 'gemini' else 'content'
            messages[-1] = {**messages[-1], key: messages[-1][key] + message[key]}
        else:
            messages.append(message)
    return messages


def tool_options(protocol, tools):
    if not tools:
        return {}
    if protocol == 'anthropic':
        return dict(tools=tools, tool_choice={'type': 'auto', 'disable_parallel_tool_use': True})
    functions = [dict(name=t['name'], description=t['description'], parameters=t['input_schema']) for t in tools]
    if protocol == 'gemini':
        return dict(tools=[{'functionDeclarations': functions}])
    return dict(tools=[{'type': 'function', 'function': f} for f in functions], parallel_tool_calls=False)


def decode_reply(protocol, data):
    calls = []
    if protocol == 'openai':
        original = data['choices'][0]['message']
        native = {k: v for k, v in original.items()
                  if k in {'role', 'content', 'tool_calls', 'reasoning_content'}}
        native['role'] = 'assistant'
        text = original.get('content') or ''
        if isinstance(text, list):
            text = ''.join(p.get('text', '') for p in text if p.get('type') == 'text')
        for call in original.get('tool_calls') or []:
            function = call['function']
            calls.append(dict(id=call['id'], name=function['name'], arguments=function['arguments']))
    elif protocol == 'anthropic':
        native = dict(role='assistant', content=data['content'])
        text = ''.join(p['text'] for p in data['content'] if p['type'] == 'text')
        for p in data['content']:
            if p['type'] == 'tool_use':
                calls.append(dict(id=p['id'], name=p['name'], arguments=p['input']))
    else:
        native = data['candidates'][0]['content']
        text = ''.join(p.get('text', '') for p in native['parts'] if not p.get('thought'))
        for p in native['parts']:
            if 'functionCall' in p:
                call = p['functionCall']
                calls.append(dict(id=call.get('id'), name=call['name'], arguments=call.get('args', {})))
    if not isinstance(text, str):
        raise ValueError('Response text must be a string')
    return dict(role='assistant', text=text, tool_calls=calls, native_protocol=protocol, native=native)


def tool_arguments(call):
    arguments = call['arguments']
    if isinstance(arguments, str):
        from harness.meta.utils.common import parse_json
        arguments = parse_json(arguments)
    if not isinstance(arguments, dict):
        raise ValueError('Tool arguments must be a JSON object')
    return arguments

# 03 Harness 机制归因

When present, read the run's `execution.json` and relevant `events.jsonl` entries.
Events link to zero-based `result.json.history` indices and tool call IDs; compare
dispatch, exit status, output clipping/discarding, API attempts and termination.
Respect `limited_files`: missing events after a recording cap are not evidence
that an action never occurred. Older runs may have no execution metadata.

Inspect the evaluated source at `/materials/current_harness/`. Map each important
behavioral divergence to the actual mechanism that supplied instructions, executed
actions, returned feedback, maintained context or accepted completion.

Use these locations relative to that root, following imports if the layout evolved:
- `task_agent.py:TaskAgent.forward`, `prompt.py`: inputs and task instructions.
- `agent/llm_withtools.py:chat_with_agent`: dispatch, budgets, completion and format recovery.
- `agent/llm.py:get_response_from_llm`: history and model-call configuration.
- `agent/tools/bash.py`, `agent/tools/edit.py`: execution, output and file editing.
- `agent/protocol.py`, `agent/providers.py`: message encoding and response handling.

Check whether the desired behavior already exists and whether its output actually
reached the model. If feedback was available but ignored, do not describe it as
missing. Determine whether a reusable harness intervention can influence the
failure, with signals available during normal execution.

For each supported target, state: trace evidence → source location → mechanism
weakness → possible intervention. Mark failures with no supported harness remedy
as unresolved. These source files are navigation hints, not a mandatory edit list.

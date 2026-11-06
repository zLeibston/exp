# 03 Harness 变更归因

When present, compare the runs' `execution.json` and relevant `events.jsonl`
entries. They link to zero-based `result.json.history` indices and tool call IDs.
Check dispatch, exit status, output clipping/discarding, API attempts and stop
reasons. Check `limited_files` before interpreting missing events; older runs
without this metadata remain usable and must not be treated as having no errors.

Compare `/materials/tasks/<id>/previous_harness/` with
`/materials/current_harness/`: these produced the historical and current runs.
Use that task entry's `evaluated_diff` when present. Otherwise `harness_diff`
compares the same versions because the starting candidate equals the evaluated
version. `harness.diff` always targets the starting candidate; it is not generally
the diff that explains the recorded regression.

Start from changed files and link a specific hunk to the observed lost behavior:
what changed in execution, available feedback, context, recovery or completion?
Check both supporting and contradicting repetitions. A textual change alone is
not evidence of causation; an empty diff can still accompany different outcomes.

Source navigation, relative to each supplied Task Harness root:
- `task_agent.py:TaskAgent.forward`, `prompt.py`: task input and instructions.
- `agent/llm_withtools.py:chat_with_agent`: dispatch, budgets, completion, format recovery.
- `agent/llm.py:get_response_from_llm`: history and model-call configuration.
- `agent/tools/bash.py`, `agent/tools/edit.py`: command execution and file editing.
- `agent/protocol.py`, `agent/providers.py`: message encoding and response handling.
Follow imports within the supplied source if this layout has evolved.

State the suspected mechanism, exact source locations and competing explanations.
Do not attribute current failures to edits made after those runs were collected.

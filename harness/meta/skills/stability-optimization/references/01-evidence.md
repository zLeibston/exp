# 01 稳定性证据梳理

Read `/materials/index.json` and `/materials/comparison.md`. The scheduler has
already selected this stage's tasks; use only `index.json.tasks`. For each task,
read `/materials/tasks/<id>/task.json` and `comparison.md`, record
`current.correct / current.evaluations` and the success/failure distribution,
and enumerate `current.trials[].trajectory`. Do not assume fixed task or run counts.

Prioritize informative contrasts: a success and a failure with comparable early
progress, then distinct failure modes. Record why each pair is useful. Inventory
all supplied tasks first, then deepen analysis in priority order; distinguish
reviewed evidence from runs not yet inspected when the tool budget limits coverage.

Runs live under `/materials/tasks/<id>/current/{success,failure}/repeat_XX/`.
Use `trajectory.md` for readable navigation and `result.json` for exact records.
Read individual files or relevant turn ranges rather than dumping all logs.
Do not seek additional evidence in other rounds, stages, `/output`, or the Meta
runtime. Missing or inconsistent evidence is a limitation, not something to invent.

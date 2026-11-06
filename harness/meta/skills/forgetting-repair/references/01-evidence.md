# 01 退化证据梳理

Read `/materials/index.json` and `/materials/comparison.md`. Enumerate only
`index.json.tasks`; for each task read `/materials/tasks/<id>/task.json` and
`comparison.md`. Record `previous.correct / previous.evaluations` and
`current.correct / current.evaluations`, with the success/failure counts and
links to the listed repetitions. Do not assume a fixed number of tasks or runs.

Each task's historical observation is its own last evaluation, possibly several
rounds earlier. Use that entry's paths, never another task's historical version.
A lower observed success count is a regression signal, not proof of a code cause.
Missing or inconsistent evidence remains an explicit limitation; do not fill gaps
from other rounds, other stages, `/output`, or the Meta runtime.

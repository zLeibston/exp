# 05 通用机制改进

Read the implicated files in the candidate path supplied by the instruction
(`/candidate/harness/task` in the current runtime) before editing. The recorded
runs describe `/materials/current_harness/`, not your modified candidate.

State the intended behavior and activation condition, then implement a focused
change to the shared mechanism. Use prompt changes when instructions are at fault;
change runtime behavior when execution or feedback is at fault. Check that required
signals exist. Prefer strengthening an existing mechanism to adding overlapping
rules or new complexity without evidence.

Preserve successful strategies, Task entry interfaces and provider contracts.
Avoid task-ID branches, task-specific filenames or answers, historical solution
snippets, unconditional retries and growing lists of one-off prompt rules.
Keep scope proportional to the evidence; do not modify Meta, materials, scoring,
model configuration or outer scheduling. Edit only the candidate Task Harness.

If no supported reusable change is available, leave the candidate unchanged and
explain why. The goal is to make demonstrated successful behavior more reliable;
no measured improvement can be claimed before subsequent task evaluation.

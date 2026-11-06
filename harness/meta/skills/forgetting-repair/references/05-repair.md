# 05 设计并实施最小修复

Form a focused repair hypothesis: observed loss → implicated mechanism → proposed
change → successful behaviors that must remain. Use evidence from all supplied
tasks to identify conflicts. Restore the lost invariant with the smallest useful
change; prefer narrowing an overbroad mechanism to removing it wholesale.

Edit only the candidate Task Harness. Preserve prior stability improvements,
current successes, alternative valid strategies, the Task entry interface and
provider contracts. Change prompts when instructions are responsible; change
runtime behavior when execution is responsible. Do not blanket-copy a historical
harness, embed historical solutions, or add task-ID/benchmark-specific branches.
Do not modify Meta, evidence, scoring, model configuration or outer scheduling.

If the code diff does not establish a cause, a reusable recovery change may still
be supported by the trajectories; state that narrower hypothesis. If there is no
justified useful repair, leave the candidate unchanged and explain the uncertainty.

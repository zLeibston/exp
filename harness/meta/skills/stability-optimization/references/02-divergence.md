# 02 关键行为分叉分析

Compare successful and failed runs of the same task under the evaluated harness
at `/materials/current_harness/`. Follow the exact trajectory paths in the manifest.
Align task obligations, decisions and intermediate outcomes, not turn or repetition
numbers: runs need not share a seed or a strategy.

Identify a consequential divergence that can explain the outcome: the action,
feedback then available, model response, later recovery attempts and eventual
failure. Check that an early error was not subsequently repaired. Different
commands or valid approaches alone do not constitute a defect.

Inspect other supplied repetitions for corroboration and counterexamples.
Record task ID, success/failure trace paths and turns, the behavioral difference,
its likely consequence and competing explanations. Keep model-choice variability,
environment feedback and harness behavior distinct until the source supports
an attribution. Small samples do not establish reliable success probabilities.

Method inspiration: [RiseHarness](https://arxiv.org/html/2610.27435v1), using
same-task success/failure contrasts and checking hypotheses against harness
behavior. This view supplies recorded trajectories and source; it does not
promise a runtime monitor, hidden grader logs or Harness Handbook.

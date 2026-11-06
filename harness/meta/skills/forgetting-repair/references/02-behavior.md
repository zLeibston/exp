# 02 跨版本行为对比

Follow `previous.trials[].trajectory` and `current.trials[].trajectory` in each
manifest entry. Start with historical successes versus current failures under
`/materials/tasks/<id>/{previous,current}/{success,failure}/repeat_XX/`.
Read `trajectory.md`; use `result.json` for exact messages, tool arguments,
outputs and additional recorded fields. Read individual files or relevant turn
ranges rather than dumping all logs at once.

Compare the same task obligations and intermediate outcomes, not matching turn
numbers or repetition numbers: runs need not share a seed or strategy. Locate
the earliest consequential divergence, the feedback then available, subsequent
recovery attempts, and the eventual failure. Do not mistake a recovered error
for the cause. Review all supplied repetitions, including historical failures
and current successes, to test alternative explanations.

Record per task the lost behavior, supporting trace paths/turns, contradicting
examples and uncertainty. Group shared patterns across tasks without forcing
unrelated failures into one diagnosis.

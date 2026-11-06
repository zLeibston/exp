# 04 检查当前候选版本

Read the relevant files in the candidate path supplied by the instruction
(`/candidate/harness/task` in the current runtime). Compare them with the same
files in `/materials/current_harness/` before editing. For example:

```bash
diff -u /materials/current_harness/prompt.py /candidate/harness/task/prompt.py
```

Choose files identified during attribution; the example is not a required edit.
For `diff`, exit status 1 means differences, while status 2 indicates an error.
There is no separately supplied evaluated-to-candidate diff; compute it from
these two visible directories when needed. `harness.diff` compares historical
source with the starting candidate, not these two versions.

Determine whether the preceding stability optimization already touches, repairs
or changes the suspected mechanism. Preserve useful improvements and adapt the
repair to the actual candidate. If it already addresses the issue, avoid a
redundant patch and record that its effect remains unverified. Do not assume
stability edits occurred when the candidate equals the evaluated source.

Candidate changes have no corresponding task runs in this view. After your own
edits, the supplied diffs still describe the starting candidate; track your
additional changes separately.

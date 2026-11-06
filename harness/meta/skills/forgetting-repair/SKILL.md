---
name: forgetting-repair
description: Repair lost task-harness behavior using per-task historical evidence, evaluated source, and the current candidate.
---

# Recover lost behavior

Start at `/materials/index.json`; use only its listed tasks and evidence paths.
Follow these six steps. Read each reference with Bash when reaching that step;
paths are relative to this skill. Keep concise findings for the final summary.

01. [退化证据梳理](references/01-evidence.md)：明确退化任务及历史、当前成功次数。
02. [跨版本行为对比](references/02-behavior.md)：对比历史成功与当前失败轨迹，定位关键分叉。
03. [Harness 变更归因](references/03-attribution.md)：对照历史源码、评测源码和 diff，定位可能的回归机制。
04. [检查当前候选版本](references/04-candidate.md)：检查先前稳定性优化是否已影响相关代码或行为。
05. [设计并实施最小修复](references/05-repair.md)：恢复丢失能力，尽可能保留其他改进。
06. [验证与小修正](references/06-check.md)：检查接口、代码与局部行为，发现问题后做最小修改并复查。

A success-count drop does not prove a code
cause; keep evaluated behavior distinct from untested candidate edits. Unsupported
repairs may result in no edit. Do not embed task solutions or blanket-revert.

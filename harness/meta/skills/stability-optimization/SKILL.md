---
name: stability-optimization
description: Improve task-harness reliability from same-harness success/failure contrasts and shared mechanism defects.
---

# Stabilize demonstrated capability

Start at `/materials/index.json`; the supplied tasks are already selected for this
stage. Follow these six steps, reading each reference with Bash when reaching it.
Reference paths are relative to this skill. Keep concise findings for the summary.

01. [稳定性证据梳理](references/01-evidence.md)：掌握各任务成功/失败分布，选择优先分析的轨迹。
02. [关键行为分叉分析](references/02-divergence.md)：对比同一 Harness 下的成功与失败轨迹，定位关键分叉。
03. [Harness 机制归因](references/03-attribution.md)：映射到具体机制和源码，判断可通过 Harness 修改解决的失败。
04. [跨任务共性归纳](references/04-shared-patterns.md)：综合诊断，识别共享缺陷，优先考虑可泛化的改进。
05. [通用机制改进](references/05-improve.md)：修改候选 Task Harness 的通用机制，避免任务硬编码与过度修改。
06. [行为检查与总结](references/06-check.md)：检查接口、代码与局部行为，发现问题后做尽可能小的修正。

Preserve successful behavior and distinguish
supported mechanisms from speculation. If no useful change is justified, leave
the candidate unchanged and explain the uncertainty.

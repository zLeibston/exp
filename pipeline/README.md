# A/B/C RSI 外层调度

`app.py` 负责配置、运行锁、快照、镜像和续跑；`evolution.py` 定义唯一启用的进化循环；`scheduling.py` 负责抽题、重复评测和统计，`materials.py` 负责分阶段材料。Harness 定义与两套修补提示词仍由 `harness/` 自己维护。旧的 archive/practice/degradation 模块及阶段入口已删除。

## 配置和启动

在 `exp/config.yaml` 中配置：

```yaml
scheduler:
  cold_start_repeats: 4
  repeats_per_task: 4
  quotas:
    A: 5
    B: 10
    C: 5
```

在 `exp/` 运行：

```bash
# 第1轮只做全池冷启动；默认使用筛选后的全部202题，共808次有效评测。
.venv/bin/python run.py --domain terminal --rounds 1 --output runs/abc-001

# 总计10轮，包括第1轮冷启动。
.venv/bin/python run.py --domain terminal --rounds 10 --output runs/abc-001 --resume

# 新开一份完整实验。
.venv/bin/python run.py --domain terminal --rounds 10 --output runs/abc-002
```

`--limit` 只截取池子，不改变每轮配额。`--rounds` 是含冷启动的目标总轮数。`--eval-only` 独立单遍评测，不运行调度、分类或修补。旧版 run 无法续跑到新算法。旧 `evolution` / `archive` / `degradation_options` YAML 与候选、初筛 CLI 已移除，不再兼容。

## 唯一运行流程

1. 第1轮用初始 Harness 对全池每题评测4遍。4/4→A，0/4→C，其余→B；记录全部结果、正确次数、评测次数及对应版本。冷启动不调用 Meta。
2. 后续每轮从当前 A/B/C 池各取5/10/5题。各池按累计评测次数从少到多排序；并列按题目ID排序。不重复取同一题，不足配额时用完该类、不跨类补齐，实际数量写入 `plan.json`。
3. 固定本轮 Harness，把全部“题目×4次”作为独立试次放入同一个流水线队列。有空闲worker和容器名额就立即启动下一个试次，不等待前一遍全部完成；同一题不同试次也可并发。全部有效评测完成后更新标签和累计统计。分类只依据本轮结果，不依据累计正确率。
4. B→B：调用 `stability_repair`，只提供这些题当前 Harness 的成功/失败轨迹和任务输入。
5. A→B/C、B→C：调用 `forgetting_repair`，只提供遗忘题的前后轨迹、相关 Harness 和 diff。修补建立在稳定修补后的版本上；诊断证据仍是本轮修补前的评测，不把它冒充为稳定修补后的表现。
6. 无相应题目就跳过该阶段。A→A、B/C→A、C→C、C→B 均不触发修补。按顺序完成两阶段后，最终版本直接用于下一轮；不额外筛选、全池评测或挑选最佳历史父版本。

每轮通常80次有效题目执行，冷启动808次（当前202题）。若两种修补都触发，最多两次 Meta 调用。Meta 可以判断证据不足而不改代码；修改必须通过 Task-only 范围、入口契约、编译和独立导入检查。Meta Harness 不在本算法的进化范围内。

## 轨迹隔离

每个修补阶段只使用一个独立容器，Meta与Bash/editor在其中运行，结束后销毁。容器包含本次Meta运行所需源码与配置、可写的`/candidate/harness/task`和只读的`/materials`，不挂载完整运行目录或完整候选快照。

`materials.py`按本阶段筛选并白名单导出材料：稳定阶段只选B→B题目的本轮轨迹；遗忘阶段只选A→B/C、B→C题目，额外提供这些题目退步前的实际轨迹、Harness及diff。不混入其他题目、无关轮次或另一修补阶段的轨迹。材料和候选使用固定容器路径。

```text
<stage>/
├── comparison.md                       # 对应题目总览
├── index.json
├── current_harness/                    # 本轮实际评测版本，只读
└── tasks/<id>/
    ├── task.json                       # 任务输入
    ├── comparison.md                   # 同题各次评测对照表
    ├── comparison.json
    ├── current/
    │   ├── summary.json
    │   ├── success/repeat_XX/
    │   └── failure/repeat_XX/
    ├── previous/                       # 仅遗忘阶段：退步前轨迹
    │   ├── success/repeat_XX/
    │   └── failure/repeat_XX/
    ├── previous_harness/               # 仅遗忘阶段：该题上次评测版本
    ├── harness.diff                    # 仅遗忘阶段：上次→本阶段开始时的候选版本
    └── evaluated.diff                  # 候选已被稳定修补修改时：上次→本轮实际评测版本
```

每个`repeat_XX/`保留完整原始`result.json`、可读`trajectory.md`、结果标记`outcome.json`及可用的Task对话和执行日志，不截断轨迹。不复制隐藏测试、verifier日志、其他题或另一阶段Meta对话。简短目录说明集中在`harness/meta/prompt.py`，按阶段发送给模型；Pipeline负责白名单导出和物理挂载。导出副本使用固定容器路径，不带A/B/C、阶段字段、轮次和全局Harness哈希；宿主机完整审计记录仍保留。每次Meta调用只占用一个执行容器名额。

遗忘题可能很多轮没有被抽到，因此逐题使用自身上次评测时的真实Harness及diff，不额外暴露无关历史版本。稳定阶段不提供旧轨迹。遗忘阶段编辑的候选已包含本轮稳定修补；材料中的`current_harness`仍是实际产生本轮轨迹的评测版本，不能把候选当作已经重新评测过。

## 保存与续跑

- `config.json`：脱敏 API、题池、镜像、资源和调度配置。
- `state.json`：原子提交的已完成轮、当前/上一评测版本、各题历史统计和最新观测索引、版本记录。`evaluation_count` 包含执行错误；`valid_evaluation_count` 为有效0/1评测数；`correct_count` 为有效正确数。
- `round_NNNN/plan.json`：评测版本、抽题、配额和重复次数。
- `evaluation/repeat_XX/attempt_XXXX/`：每次独立尝试的原始评测报告和逐题轨迹。
- `labels.json`：本轮更新后的统计、转换和阶段归属；在 Meta 前落盘。
- `stability_repair/`、`forgetting_repair/`：候选代码、diff、Meta轨迹、检查日志和阶段完成收据。

执行/API/评分报错单独记录，不判为0/4，不进入 Meta。遇到错误暂停本轮；修复环境后用 `--resume` 只重试失败/未完成的试次。有效试次不重复调用、不重复计数；错误尝试仍保留并计入累计尝试数。单个普通执行错误不阻止本轮其他已排队试次完成；整轮收集后暂停，续跑只补缺失或失败的试次。中断的未验证 Meta 候选保留，新尝试另建目录；已经完成并通过检查的修补阶段通过收据复用。

本算法固定保留历史证据，不再提供清理开关。续跑固定题池、调度设置、API公开参数、评测时限与容器资源上限；允许调整并发数及目标总轮数。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/terminal_docker_smoke.py
```

使用本地模拟模型，不调用真实付费 API。单元回归检查全部标签转换、最少评测抽样、配额不足、统计、冷启动、阶段证据内容、错误与中断恢复；Docker 冒烟运行真实 Task/Meta worker、工具及评分，并检查两个阶段的实际挂载隔离。

## 领域边界

任务加载通过 `tasks.load_domain(domain, split=..., dataset=...)`；环境、执行和可信评分通过 `TaskSuite` 回调。Pipeline不导入 `tasks.terminal` 或判断领域名。Terminal数据位于 `tasks/terminal/data/`，筛选记录位于 `audit/`，镜像存储位于 `env/`。`--domain terminal --dataset tb`/`--dataset tbh` 可单独选来源，默认合并。新增领域只需提供领域包入口；协议见 [../tasks/README.md](../tasks/README.md)。

本次目录迁移会改变任务绝对路径和领域元数据。旧运行保留，但不能将旧缓存直接续跑到新目录布局；新实验请新开run。

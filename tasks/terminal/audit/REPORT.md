# TB + TBH 进化池静态筛选记录

TB2.1 对照版本：`12b7565679ce5006230b820666353673a133db93`，89 道题。

原始 220 题；明确排除 15 题；剩余 205 道候选，其中 3 道需要进一步确认。候选不代表已通过运行验收。

## 筛选标准

- 排除明显任务近似变体，而不只排除逐字重复。共享一般技能不足以判重复。
- 排除有具体证据的测试暴露、错误判定条件和关键功能未验证。
- 不因为没有参考解、题难、需要联网或编译成本高而自动排除。
- 原始题目未删除。`candidates.json` 是池清单，不是现有 `--tasks` 的题目输入格式；当前 pipeline 尚未接入它。

## 明确排除

| 来源 | 任务 | 原因 |
|---|---|---|
| tb | `draft_dp_d150f869_outb` | 相同 /app/ssl 文件、2048 位 RSA、365 天有效期，且组织名 DevOps Team、域名 dev-internal.company.local 完全一致。 对应 TB2.1 `openssl-selfsigned-cert`。 |
| tb | `draft_dp_9793b5ad_outb` | 相同三份数据输入路径、三源优先级和两个输出文件；字段与报告格式有变化，仍属高度近似实例。 对应 TB2.1 `multi-source-data-merger`。 |
| tb | `draft_dp_1e43823f_outa` | 三份输入、字段映射、优先级、输出路径和逐字段冲突报告高度一致。 对应 TB2.1 `multi-source-data-merger`。 |
| tb | `draft_dp_027e1b7a_aopeng` | 相同 pyknotid 0.5.3、NumPy 2.3.0、仓库、路径和三个 Cython 扩展。 对应 TB2.1 `build-cython-ext`。 |
| tb | `draft_dp_2fda0f83_outa` | 相同人物邮箱、Jan 15–19 2024、日历文件、会议名、输出文件及多项个人时间约束。 对应 TB2.1 `constraints-scheduling`。 |
| tb | `draft_dp_2fda0f83478_outa` | 同一日历排程场景与人物、日期、输出；增加 RRULE、时区和 buffer 条件，按近似变体排除。 对应 TB2.1 `constraints-scheduling`。 |
| tb | `draft_dp_a1797499_outa` | 相同 /app/run.py、run_tasks(tasks,max_concurrent) 接口、SIGINT 取消和异步清理要求；训练题扩展了边界条件。 对应 TB2.1 `cancel-async-tasks`。 |
| tb | `draft_dp_0c5cabc8_haitian` | environment 中含官方测试相同副本，Dockerfile COPY . 将其放入 /app/setup_files/，无 .dockerignore 排除。 |
| tb | `draft_dp_caa7cf97_haitian` | environment 中含官方测试相同副本，Dockerfile COPY . 将其放入 /app/，无 .dockerignore 排除。 |
| tb | `draft_dp_0004e5bb_aopeng` | 拆分合并测试主要核验路径、文件大小和总行数，不比较原始与重建数据值；保持行数的错误实现可能被接受。 |
| tb | `draft_dp_d84934e9_outb` | 题目要求对所有黑方应对均强制将死；测试记录 all_responses_lead_to_checkmate，却只断言 checkmate_found，实际把全称条件降成存在条件。 |
| tbh | `tbh_task_4c14564e2ec33ce5` | 要求固定 seed 的 Duffing 蒙特卡洛数值模拟，但测试仅检查脚本存在、三位小数及结果落在 (0,1)；空脚本加 0.500 已在隔离路径下通过全部两个原始测试函数。 |
| tbh | `tbh_task_7fee0e182f2ca1cd` | 不运行 sanitizer，也不核验实际 instance_id/覆盖率；假 ID 的两行 ACCEPT/REJECT 和空 benchmark.sh 已通过全部四个原始测试函数。 |
| tbh | `tbh_task_04836d64e7ccc952` | 题目明确要求拒绝非法 Base64，但唯一功能调用只反复验证全 A 的合法输入；恒返回 true 的 addon 无法被这些测试区分。0.25 秒阈值另列为资源敏感风险。 |
| tbh | `tbh_task_d575b3ecf9318243` | 要求抢救处理器临时生成的 1000 份数据；测试只要求 850 份内容不同且自身哈希匹配的文件，未核验它们属于处理器输出，预造任意文件也满足最终判分条件。 |

## 待核实（仍列在候选清单）

- `tbh/tbh_task_036f8f2f6b7ae8fc`：bindfs /proc/mounts 检查依赖 FUSE 与挂载权限；普通受限 Docker 不保证支持，先验证运行条件，不按质量缺陷直接删除。
- `tbh/tbh_task_3280bade2c98c2d1`：题目要求 unshare -n；需验证 namespace 权限，不需要嵌套虚拟化。
- `tb/crypto-transaction-batching-optimizer`：与 TB2.1 llm-inference-batching-scheduler 存在调度模板结构相似，但应用对象和规则不同，暂不认定明显重复；保守无污染实验可先隔离。

## TB / TBH 跨来源去重

没有发现逐字相同的指令或 Python 测试集合；高相似候选人工核对后，尚未确认明显重复。比如安全解压与 Go 归档重命名共享技能，但任务目标与验收不相同，两者保留。

## 证据与限制

- `selection.json` 给出本地文件、行号、证据摘录和排除理由。
- `tbh-proof-results.json` 记录两道题的反例：只重定位绝对路径，保留原测试断言；没有运行完整 Docker 环境或模型。
- Base64 负例缺失、数据抢救缺少来源校验、棋题全称/存在条件混淆等属于静态代码证据，未声称已执行完整反例容器。
- 没有把 TB2.1 测试代码或参考答案复制进进化池；对照缓存保留在 /tmp，不应向 Task/Meta 提供筛选报告。
- 若要更严格地保证实例独立，还需检查所有题的输入资产与来源，文本筛选不能证明完全无重叠。

## 环境镜像迁移（已恢复）

此前86道入池TB题因阿里云实例连接超时暂时排除，现已将基础镜像替换为官方GHCR的Python 3.13 / Ubuntu 24.04固定amd64 digest并恢复入池。只修改86个环境Dockerfile的FROM行，任务指令、测试和既有质量/重复筛选不变。`base_image_migrations.json` 保存原镜像、新镜像和修改前后文件SHA256；`runtime_exclusions.json` 保留历史排除记录，当前临时排除为0。

两个上游镜像均已拉取，Python和Ubuntu各一道代表题环境已构建并探测可用；不声称86题均已完成真实评测。阿里云副本仍不可访问，因此未能核验与上游镜像字节级相等。当前训练池恢复为202题：TB108、TBH94。题池和环境已变化，需新开运行。

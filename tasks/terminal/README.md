# TB / TBH 进化数据接入

`--domain terminal` 合并本目录的 `data/tb` 与 `data/tbh`；加 `--dataset tb` 或 `--dataset tbh` 选择单一来源。
默认读取 `tasks/terminal/audit/candidates.json`，不重新纳入已排除题，也跳过其中 `review_before_run` 的 3 题。此前依赖阿里云镜像的86道TB题已改用固定digest的官方GHCR上游基础镜像并恢复（记录在 `audit/base_image_migrations.json`）。因此当前默认池为 **202 题：TB 108、TBH 94**。所有220道原题均保留在 `data/`，目录迁移已逐题核对内容与权限哈希。镜像替换记录留在 `audit/`。

外循环现已使用 A/B/C 重复评测调度：冷启动全池每题4遍，后续每轮按类抽题、每题4遍，先稳定修补，再遗忘修补。配置与算法见 [调度文档](../../pipeline/README.md)。TB2.1 不进入此训练加载器。

## 运行

在 `exp/` 下，使用现有 API YAML：

```bash
# 小样本验证接口（会调用 Task 模型，不修改 Harness）
.venv/bin/python run.py --domain terminal --limit 2 --eval-only --timeout 600 --output runs/terminal-check

# 冷启动加一轮修补；配额和重复次数来自 scheduler YAML
.venv/bin/python run.py --domain terminal --limit 20 --rounds 2 --timeout 600 --output runs/terminal-001

# 同一运行续跑；需保留原设置，rounds 为目标总轮数
.venv/bin/python run.py --domain terminal --limit 20 --rounds 5 --timeout 600 --output runs/terminal-001 --resume
```

`--limit` 目前按清单顺序取题，先 TB 后 TBH，不是分层抽样；省略则使用整个 202 题池。原始两个训练集都只支持 `--split train`。

这台机器建议先将 YAML 的 `execution.max_containers` 设为 2 或 3。保留现有并发队列，但每题单独占用容器名额，解题与评分共享该名额；不会为每个线程创建无限量容器。任务 CPU/内存要求与全局配置取较小值。

## 执行约定

- 使用任务 `environment/` 构建镜像，**不把顶层 tests、solution、task.toml 或筛选报告放进 Agent 环境**。
- 首次实际用到某题才构建它的镜像，不在启动时构建全池。构建输出记录在该题的 `build.log`。
- 独立镜像层提供模型 API 所需的 httpx/PyYAML；不依赖 Harbor Python 包，也不读取 TBH 的 Parquet 元数据。
- 每次评测创建全新容器，挂载所选 Task Harness 与固定 worker；文件、系统软件和后台服务一直保留到评分结束。
- Agent 结束后才复制 `tests/`，运行原始 `tests/test.sh`，读取 `/logs/verifier/reward.txt`。只接受 0/1。缺失或非法奖励、超时和 worker 失败记作执行错误，不伪装成普通未解出。
- 评分结束或发生异常后销毁整个容器，保留聊天/API 日志、评分日志、状态和镜像身份，不保存完整容器文件系统。
- 同一运行首次使用镜像后，在 `image_pins/` 固定 image ID。续跑不能静默使用重新构建的不同镜像；清理镜像前保留原镜像，或启动新运行。
- 每题源码都做内容/权限哈希；源码变化会被运行配置对比或执行时校验发现。

## 联网和 Harness

Task 与 verifier 遵循原题 `task.toml` 的网络配置。本地 TB 120 题、TBH 100 题均显式设置 `allow_internet=true`，因此使用 Docker bridge 允许联网；原题要求的依赖、源码和数据由 Agent 自行下载，评分脚本仍按原样运行。程序负责题目文件、镜像拉取和 Dockerfile 中的环境准备，不代替 Agent 完成题目要求的安装、编译或修复。

未填写 `allow_internet` 时遵循 Harbor 旧格式默认值 true；明确设置 false 则使用 API-only 隔离（容器只有 loopback，主机代理仅允许模型 API）。也支持 `network_mode=public/no-network`，未知模式直接报错。这里适配的是数据自带的共享容器评分流程，不将其改造成新版 Terminal-Bench 的独立 verifier 流程。Meta 保留 API-only 策略。此前强制断网的运行不能与当前分数混用，需新开运行。

TaskAgent 仅对 `domain=terminal` 启用已有 Bash/editor；通用任务提示词不声明网络权限，实际权限由执行器按题目配置控制。模型适配、工具实现、调用预算和 Meta Harness 均保留。

任务本身可能修改系统级路径或启动服务，因此不能套用“只收集 Git patch”的提交方式，也不能在评分前清理所有后台进程。最终以容器状态和官方测试奖励计分，回复文本不是答案提交。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v

# 独立 Docker 冒烟；会构建小型 fixture 镜像，仅调用本地模拟 API
.venv/bin/python tests/terminal_docker_smoke.py
```

回归覆盖筛选清单、域选择、源码变化、资源上限、奖励解析及现有评测接口。端到端 Docker 冒烟使用本地模拟 API：真实 worker 和工具修改文件、启动服务，随后运行隐藏测试。明确断网时验证 HTTP/HTTPS 代理访问被拒、直接 IP 连接被拒、容器仅有 loopback；允许联网时验证存在网络接口，并可绕过代理直接访问宿主机测试 HTTP 服务。另覆盖失败奖励、缺失奖励和超时清理；没有调用付费模型，未声称全池题目已通过运行验收。

此外，Docker 冒烟实际调用 app/evolution 跑两轮本地 fixture：全池4遍冷启动 → 按类抽样并评测4遍 → 稳定修补 → 遗忘修补 → checkpoint及续跑；验证两阶段只挂载对应题目的证据。测试模型仅为本地 HTTP fixture，不读取真实 API 配置。

## 目录与公共接口

本领域代码、题目、筛选记录、环境存储均在本目录。`load(split=..., dataset=...)` 返回公共 `TaskSuite`，环境、执行和评分由 `runtime.py` 回调实现；Pipeline只依赖公共接口。完整协议与其他领域接入示例见 [../README.md](../README.md)。

当前Docker存储配置为 `env/docker/` 与 `env/containerd/`；系统服务路径已同步更新。它们属于本机同一个Docker守护进程的全局存储，不是每个领域独立的Docker实例；公共领域接口不依赖这些物理路径。详见 [env/README.md](env/README.md)。

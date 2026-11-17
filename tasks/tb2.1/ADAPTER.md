# TB2.1 领域适配

`--domain tb2.1` 加载原始 `registry.json` 中的89题，使用统一的 `TaskSuite` 接口，支持现有 A/B/C 进化、阶段验证及断点续跑。

目录：`tasks/` 为下载的题目；`loader.py` 加载与校验题目；`runtime.py` 复用 terminal 的容器执行、日志和可信评分。Caffe 题面补充了 CIFAR-10 的 MindSpore 镜像下载命令；两种 Harness 对比须使用同一题面。Dockerfile 和测试保持下载时的内容。

环境按需准备：拉取各题 `task.toml` 的 `environment.docker_image`，加装 agent 运行依赖，随后复用缓存。首次使用的镜像 ID 保存在 run 的 `image_pins/`，续跑要求同一镜像。构建日志、轨迹和评分日志均在 `runs/` 中。Docker 的物理存储仍由本机统一的 Docker 服务管理，位于 `tasks/terminal/env/`，不为每个领域另起 daemon。

agent 只收到题面和 terminal 工具能力，不收到测试、解答或任务配置。测试在 agent 结束后才复制入容器，沿用每题官方网络设置、CPU/内存需求和时限，并受运行配置的全局上限约束。

统一使用根目录的 `config.yaml`，其中，单容器内存上限8GB，总容器名额2（1个任务名额、1个辅助名额），以容纳89题中8道声明8GB内存的任务。各领域共用这份 API、调度及资源配置。开跑：

```bash
cd /home/ubuntu/RSI/exp
.venv/bin/python run.py --domain tb2.1 --config config.yaml \
  --rounds 20 --timeout 12000 --output runs/tb21-rsi-001
```

`--timeout 12000` 作为总上限，让适配器保留各题声明的 agent 时限（最长12000秒）。评分使用各题声明时限与配置中的最小时限两者较大值。

续跑在同一命令后加 `--resume`。只评测可用 `--eval-only --split test`；`train/val/test` 都引用同一89题，不代表彼此独立的划分。

修改了共享执行源码后，已有旧 run 的源码指纹校验会拒绝续跑；旧 run 数据保留。

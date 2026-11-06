# 网络与材料范围

TB/TBH的Task与评分沿用原题共享容器，网络按`task.toml`配置。本地数据均显式允许联网；执行器使用Docker bridge，保留Agent下载依赖、源码和数据的能力。镜像拉取与构建也允许联网。Task容器只挂载Task Harness。

明确断网的Task和Meta使用`--network none`。`network.py`通过Unix socket与容器内loopback relay只转发当前角色的模型API目标，其他代理目标返回403，直接IP出站不可用；代理不记录密钥、请求头或响应内容。

每次Meta修补只有一个容器，Meta程序和Bash/editor都在里面运行。容器挂载本次所需Meta源码及API配置、一个可写Task候选和白名单导出的只读材料，不挂载完整运行目录、原始题库或Docker socket。

稳定阶段只提供对应题目的本轮轨迹和当前Harness。遗忘阶段只额外提供这些退步题上次评测时的轨迹、历史Harness及diff，不混入其他轮次的无关版本或其他阶段的对话。两阶段分别新建容器、清空对话与工具状态。

`lifecycle.py`管理运行锁、容器预算、中断清理与续跑回收。一次Meta调用只占用一个执行容器名额；代码检查在调用结束后进行。

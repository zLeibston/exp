# A/B/C RSI

当前外循环按重复评测的 A/B/C 标签调度，针对稳定性与遗忘分别修补。默认使用筛选后的 TB/TBH 合并池 **202题**，第一轮全池每题4遍，之后每轮 A/B/C 各抽5/10/5题、每题4遍，先稳定修补，再遗忘修补。

完整算法、YAML、启动/续跑和轨迹隔离说明见 [pipeline/README.md](pipeline/README.md)。领域数据统一位于 `tasks/<domain>/`，公共接入协议见 [tasks/README.md](tasks/README.md)。Terminal镜像与网络沿用原题配置，见 [终端任务说明](tasks/terminal/README.md)。

`harness/task/`、`harness/meta/` 分别维护独立 Agent、工具、API 适配和 `prompt.py`。本算法只有 Meta 编辑 `harness/task/`，不包含通用优化或 Meta 自进化；调度、统计、视图与可信评分留在 `pipeline/` 和 `tasks/`。

## 运行

```bash
cd /home/ubuntu/RSI/exp
.venv/bin/python run.py --domain terminal --rounds 10 --output runs/abc-001
# 同一运行续跑到总计20轮
.venv/bin/python run.py --domain terminal --rounds 20 --output runs/abc-001 --resume
```

第1轮为冷启动，至少 `--rounds 2` 才进入按标签抽题的修补轮。旧流程运行需要新开 run。

安装依赖：`.venv/bin/python -m pip install -r requirements.txt`。

## 独立 API 配置

```yaml
task_agent: deepseek
meta_agent: claude
```

首次使用先执行 `cp config.example.yaml config.yaml`；真实 `config.yaml` 不纳入 Git。
模板包含 `claude / deepseek / gpt / gemini` 预设和官网 URL。
在 `providers` 对应条目填写 key 后，只改上面两行即可切换，未选中的 key 可留空。
如果同一家需要两套密钥，可以分别写 `task_agent: {use: claude, api_key: "TASK_KEY"}`
和 `meta_agent: {use: claude, api_key: "META_KEY"}`；模型、URL 也可按角色覆盖。
原先每个角色直接配置 api_key/url/model 的写法仍兼容。

模型名示例仅展示填写格式，实际填写账号可用的模型。

| 服务 | URL 示例 | 协议 |
| --- | --- | --- |
| GPT / OpenAI | `https://api.openai.com/v1` | OpenAI Chat Completions |
| DeepSeek | `https://api.deepseek.com` | OpenAI Chat Completions |
| Claude / Anthropic | `https://api.anthropic.com/v1` | 原生 Messages |
| Gemini / Google AI Studio | `https://generativelanguage.googleapis.com/v1beta` | 原生 generateContent |
| OpenAI 兼容网关，包括代理 Claude/Gemini | `https://your-gateway.example/v1` | OpenAI Chat Completions |

`provider: auto`（默认）根据官方域名识别原生协议；其他 URL 默认 OpenAI 兼容协议。
无 URL 时按模型前缀选官方地址。自定义原生网关可显式设置 `provider: anthropic` 或 `provider: gemini`。
URL 填对应服务的 API base；也支持完整的 `/chat/completions`、`/messages`、`:generateContent` 地址。
此处 Gemini 是 API key 认证的 Google AI Studio 接口；不包含 Vertex AI IAM、Azure 部署认证或 OpenAI Responses 专用模型。

任务评测并发在 config.yaml 中显式配置：

```yaml
evaluation:
  workers: 50
```

每次重复评测使用统一任务队列。TB/TBH 每题、每次评测均使用新容器，完成评分后销毁；实际并发还受 execution.max_containers 限制。

默认总输出预算为 16384 tokens；当前 DeepSeek 预设显式设置 reasoning_effort: low，Task / Meta 均继承该设置。`providers.deepseek.json_mode` 控制 DeepSeek 原生 JSON 模式，默认 true；false 时不发送 `response_format`，仍按提示词要求输出裸 JSON。Gemini 的 `json_mode` 控制 `generationConfig.responseMimeType: application/json`；其余厂家不附加 JSON 模式参数。

可选参数：`max_tokens`、`temperature`、`timeout`（单次请求秒数）、`retries`（网络/服务错误的额外重试次数）、`reasoning_effort`（OpenAI兼容接口的思考强度）、`budget_fallback`（DeepSeek预算耗尽时是否关闭思考重试一次，默认true）。
默认不传 temperature，避免推理模型拒绝采样参数；GPT-5/o 系列自动使用 `max_completion_tokens`。
不支持的自定义模型参数不会被猜测或静默丢弃。外层 `--timeout` 仍是一次完整 agent 调用的总时限，请给多轮工具调用留足预算。

Task 和 Meta 的 API 适配层分别位于 `harness/task/agent/providers.py`、`harness/meta/agent/providers.py`，各自提供 `complete(config, messages) -> str`。
三种协议的认证、历史格式、文本提取与工具循环均在各自目录独立实现；重复代码有意保留，修改一侧不会影响另一侧。
协议参考：[OpenAI](https://developers.openai.com/api/reference/resources/chat)、[DeepSeek](https://api-docs.deepseek.com/)、[Anthropic](https://docs.anthropic.com/en/api/messages)、[Gemini](https://ai.google.dev/api/generate-content)。


调度与恢复说明以 [pipeline/README.md](pipeline/README.md) 为准。

仓库不包含下载的题库、镜像存储和运行产物。运行 Terminal 领域前，需在
`tasks/terminal/data/tb/`、`tasks/terminal/data/tbh/` 放置本地对应数据；目录结构见
[终端任务说明](tasks/terminal/README.md)。

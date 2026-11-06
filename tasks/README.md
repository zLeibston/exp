# 领域适配与统一评测接口

```text
tasks/
├── __init__.py         # load_domain：发现领域，不维护领域分支
├── base.py             # TaskSuite 及通用题目格式
└── terminal/
    ├── __init__.py     # 导出 load(split=..., dataset=...)
    ├── loader.py       # TB/TBH加载、筛选及内容校验
    ├── runtime.py      # 任务镜像、容器执行和可信评分
    ├── data/
    │   ├── tb/         # 120道原始题目及manifest
    │   └── tbh/        # 100道原始题目及元数据
    ├── audit/          # 质量筛选、去重、镜像替换与迁移记录
    └── env/            # 当前本机Docker/containerd持久存储
```

当前训练池为202题（TB108、TBH94）；所有220道原题都保留，筛选状态没有因目录迁移改变。

## 公共领域入口

```python
from tasks import load_domain
from pipeline.evaluation import evaluate
from pathlib import Path

suite = load_domain('terminal', split='train', dataset=None, limit=20)
images = suite.setup()
result = evaluate(workspace, Path('artifacts/evaluation'), model, timeout=600,
                  suite=suite, workers=2)
```

`workspace` 是包含 `harness/` 的版本目录，`model` 是 Pipeline 已解析的运行配置。正式运行由 `pipeline.app.run` 调用相同接口，并管理运行锁、容器预算、API配置和快照；`evaluate` 不包含 A/B/C 调度策略。

`evaluate` 返回 `score`、逐题 `cases`、`trajectory_path` 等字段。每题的 `passed` 必须为布尔值；`error=None` 表示有效评测。准备、执行或评分异常作为错误记录，由调度器决定暂停及恢复，不伪装成答错。

## 新领域接入

新增 `tasks/<domain>/`，在 `__init__.py` 导出：

```python
from tasks import TaskSuite

def load(split='train', dataset=None):
    return TaskSuite(
        name='my_domain', split=split, cases=load_cases(split, dataset),
        prepare_case=prepare, task_runner=run_task, grade_case=grade,
        setup_environment=setup, network_policy='my-domain-policy-v1',
    )
```

`my_domain` 必须与包名一致。入口接口如下：

| 回调 | 输入 | 返回/职责 |
| --- | --- | --- |
| `setup_environment` | `cases` | 环境/镜像描述字典；可提前准备，也可声明按需构建 |
| `prepare_case` | `case, directory` | 当前试次的Agent输入；省略时复制 `case['inputs']` |
| `task_runner` | `workspace, directory, model, timeout, inputs` | 在本领域环境执行，返回 `{'prediction': ..., 'history': ...}` |
| `grade_case` | `case, prediction, directory` | 可信评分，返回 `{'passed': bool, 'error': None或错误信息}` |

每题至少包含安全且唯一的 `id` 和 `inputs` 字典；额外字段由领域适配器维护。同一题的不同重复试次可以并发执行，运行器必须以 `directory` 区分工作区，不能共享可写任务状态。运行器负责独立环境、资源/时限、日志导出与清理；可信评分必须留在领域适配器，不能放入可进化Harness。准备步骤中的私有输入也由运行器过滤，不能向Agent泄露评分数据。

逐题日志保存在 `directory`。供Meta修补使用的通用轨迹文件为 `chat.log`、`result.json`、`stdout.log`、`stderr.log`；环境与评分的私有文件不进入Meta材料。`result.json` 使用上面的Agent返回格式。无错误但没有解出，应返回 `passed=False, error=None`。

接入后直接运行：

```bash
.venv/bin/python run.py --domain my_domain --rounds 10 --output runs/my-domain-001
```

不需要修改Pipeline的领域分支或CLI选项。模型/工具能力由各自Harness维护，公共评测接口不自动替新领域定义Harness行为。

Terminal具体配置见 [terminal/README.md](terminal/README.md)，外层算法见 [../pipeline/README.md](../pipeline/README.md)。

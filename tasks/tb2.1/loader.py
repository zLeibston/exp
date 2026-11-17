"""Load the downloaded Harbor registry through the common TaskSuite interface."""
import json
from pathlib import Path
import tomllib

from tasks import TaskSuite
from tasks.terminal.loader import task_digest
from tasks.terminal import runtime as terminal
from . import runtime

ROOT = Path(__file__).resolve().parent


def load(split='train', *, dataset=None, root=ROOT):
    if split not in {'train', 'val', 'test'}:
        raise ValueError('TB2.1 split must be train, val or test')
    if dataset not in {None, 'tb2.1'}:
        raise ValueError('The tb2.1 domain contains only the tb2.1 dataset')
    root = Path(root).resolve()
    registry = json.loads((root / 'registry.json').read_text())
    if len(registry) != 1 or registry[0]['name'] != 'terminal-bench-2.1':
        raise ValueError('Expected the Terminal-Bench 2.1 registry')
    cases = []
    for entry in registry[0]['tasks']:
        name = entry['name']
        if Path(name).name != name or name in {'.', '..'} or entry['path'] != f'tasks/{name}':
            raise ValueError('Invalid TB2.1 registry entry')
        path = root / 'tasks' / name
        if path.resolve() != path:
            raise ValueError(f'TB2.1 task directory must not be a symlink: {name}')
        for required in ('instruction.md', 'task.toml', 'environment/Dockerfile', 'tests/test.sh'):
            if not (path / required).is_file():
                raise ValueError(f'Missing TB2.1 task file: {path / required}')
        config = tomllib.loads((path / 'task.toml').read_text())
        env = config.get('environment', {})
        if env.get('gpus', 0) or env.get('mcp_servers'):
            raise ValueError(f'Unsupported GPU/MCP environment: {name}')
        if not isinstance(env.get('docker_image'), str) or not env['docker_image']:
            raise ValueError(f'Missing TB2.1 prebuilt image: {name}')
        # Terminal is the Task Harness capability; the suite identity stays tb2.1.
        # No verifier, solution or environment metadata enters the agent request.
        inputs = dict(domain='terminal', source='tb2.1', task_id=name,
                      problem_statement=(path / 'instruction.md').read_text(),
                      test_description='Complete the task inside this container. Files, installed software and '
                                       'running services are graded after your response; response text alone is not a submission.')
        cases.append(dict(id=f'tb21__{name.replace(".", "_")}', language='python', source='tb2.1',
                          task_path=str(path), data_hash=task_digest(path), inputs=inputs))
    # These are alternate uses of the same 89 tasks, not disjoint dataset splits.
    return TaskSuite(cases=cases, name='tb2.1', split=split, prepare_case=terminal.prepare,
                     grade_case=terminal.grade, task_runner=runtime.invoke_task,
                     setup_environment=terminal.setup_environment,
                     network_policy=terminal.TASK_NETWORK_POLICY,
                     environment_ready=terminal.environment_ready,
                     prepare_environment=runtime.prepare_environment,
                     execution_dependencies=('terminal',))

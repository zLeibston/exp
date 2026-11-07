"""One frozen, reviewed pool; original task trees are never copied into agent context."""
import hashlib
import json
from pathlib import Path
import tomllib

from tasks import TaskSuite

ROOT = Path(__file__).resolve().parent
SOURCES = {'tb', 'tbh'}


def task_digest(path):
    digest = hashlib.sha256()
    for file in sorted(path.rglob('*')):
        if '__pycache__' in file.parts or '.cache' in file.parts:
            continue
        if file.is_symlink():
            raise ValueError(f'Task source must not contain symlinks: {file}')
        if file.is_file():
            digest.update(str(file.relative_to(path)).encode() + b'\0')
            digest.update(str(file.stat().st_mode & 0o777).encode() + b'\0')
            digest.update(file.read_bytes())
    return digest.hexdigest()


def load(split='train', *, dataset=None, root=ROOT):
    from tasks.terminal import runtime
    if split != 'train':
        raise ValueError('TB/TBH are evolution training data only; TB2.1 is a separate held-out evaluation')
    if dataset is not None and dataset not in SOURCES:
        raise ValueError('Terminal dataset must be tb or tbh')
    sources = SOURCES if dataset is None else {dataset}
    root = Path(root).resolve()
    selection = json.loads((root / 'audit/candidates.json').read_text())
    if not isinstance(selection, list) or not selection:
        raise ValueError('Terminal pool selection must be a nonempty list')
    cases, seen = [], set()
    for entry in selection:
        source, name = entry['source'], entry['task_id']
        if source not in SOURCES or Path(name).name != name or name in {'.', '..'}:
            raise ValueError('Invalid terminal pool entry')
        if source not in sources:
            continue
        # Privileged mount / namespace tasks and a possible benchmark near-match
        # and unreachable image tasks stay quarantined until explicitly restored.
        if entry.get('status') in {'review_before_run', 'temporarily_excluded'}:
            continue
        identifier = f'{source}__{name}'
        if identifier in seen:
            raise ValueError(f'Duplicate terminal task: {identifier}')
        seen.add(identifier)
        # Derive the path, rather than trusting an arbitrary manifest path.
        path = root / 'data' / source / 'tasks' / name
        if path.resolve() != path or not path.is_dir():
            raise ValueError(f'Invalid terminal task directory: {path}')
        for required in ('instruction.md', 'task.toml', 'environment/Dockerfile', 'tests/test.sh'):
            if not (path / required).is_file():
                raise ValueError(f'Missing terminal task file: {path / required}')
        instruction = (path / 'instruction.md').read_text()
        if hashlib.sha256(instruction.encode()).hexdigest() != entry['instruction_sha256']:
            raise ValueError(f'Task instruction changed since pool review: {identifier}')
        config = tomllib.loads((path / 'task.toml').read_text())
        environment = config.get('environment', {})
        if environment.get('gpus', 0):
            raise ValueError(f'GPU execution is not supported by this local adapter: {identifier}')
        # Keep tests, solution, metadata and task.toml out of the worker request.
        inputs = dict(domain='terminal', source=source, task_id=name,
                      problem_statement=instruction,
                      test_description='Complete the task inside this container. Files, installed software and '
                                       'running services are graded after your response; response text alone is not a submission.')
        cases.append(dict(id=identifier, language='python', source=source, task_path=str(path),
                          data_hash=task_digest(path), inputs=inputs))
    if not cases:
        raise ValueError('No selected terminal tasks for these sources')
    return TaskSuite(cases=cases, name='terminal', split='train', prepare_case=runtime.prepare,
                     grade_case=runtime.grade, task_runner=runtime.invoke_task,
                     setup_environment=runtime.setup_environment, network_policy=runtime.TASK_NETWORK_POLICY,
                     environment_ready=runtime.environment_ready, prepare_environment=runtime.prepare_environment)

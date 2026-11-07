"""Snapshot, artifact, history and progress bookkeeping."""
from contextlib import contextmanager
from datetime import datetime
from threading import Lock
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
from pipeline.docker_eval import validate_artifacts

SNAPSHOT_ITEMS = ("harness",)
_LOCK = Lock()

@contextmanager
def run_lock(output, resume):
    if not resume:
        output.mkdir(parents=True)  # Atomic: two new runs cannot claim the same directory.
    with (output / '.run.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f'Another process is already using this run: {output}') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)
    return path


def snapshot(source, target):
    for entry in ("task/task_agent.py", "meta/improve_agent.py"):
        if not (source / "harness" / entry).is_file():
            raise ValueError(f"Expected an independent harness snapshot with harness/{entry}: {source}. "
                             "Legacy shared harness snapshots require migration before use.")
    target.mkdir(parents=True)
    for item in SNAPSHOT_ITEMS:
        src = source / item
        if src.is_dir():
            shutil.copytree(src, target / item, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, target / item)
    return target


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execution_manifest(root, domain):
    """Fingerprint fixed execution code, excluding Harness snapshots and task data."""
    root = Path(root)
    files = {root / 'run.py', root / 'requirements.txt', root / 'containers/agent.Dockerfile',
             *(root / 'tasks').glob('*.py')}
    for directory in (root / 'pipeline', root / 'tasks' / domain):
        for parent, directories, names in os.walk(directory):
            directories[:] = sorted(name for name in directories
                                    if name not in {'__pycache__', 'data', 'env', 'audit', '.git', '.venv'})
            files.update(Path(parent) / name for name in names if name.endswith('.py'))
    return {str(path.relative_to(root)): digest(path) for path in sorted(files)}


def harness_digest(workspace):
    files = inventory(workspace / "harness")
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def inventory(workspace):
    validate_artifacts(workspace)
    return {str(p.relative_to(workspace)): dict(sha256=digest(p), mode=p.stat().st_mode & 0o777) for p in workspace.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def label(path):
    path = Path(path)
    for ancestor in (path, *path.parents):
        if ancestor.name.startswith('round_'):
            return str(path.relative_to(ancestor.parent))
    return path.name


def log(message):
    with _LOCK:
        print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def report_index(report):
    """Small navigation view; the authoritative report and traces stay on disk."""
    if not isinstance(report, dict) or 'cases' not in report or not report.get('trajectory_path'):
        return report
    directory = Path(report['trajectory_path'])
    result = {key: value for key, value in report.items() if key not in {'cases', 'task_groups'}}
    result['report_path'] = str(directory / 'summary.json')
    result['cases'] = [dict(task_id=case['task']['id'],
        trajectory_path=str(directory / case['task']['id']), **{
            key: case[key] for key in ('passed', 'error', 'status') if key in case})
        for case in report['cases']]
    return result

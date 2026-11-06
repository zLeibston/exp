"""Run-scoped Docker ownership, cancellation and recovery under the run-directory lock."""
from contextlib import contextmanager
import hashlib
import json
import math
import re
import shutil
import tempfile
from pathlib import Path
import signal
import subprocess
from threading import BoundedSemaphore, Event, current_thread, main_thread

_active = {}
_budgets = {}
_limits = {}
DEFAULT_EXECUTION = dict(max_containers=3, cpus=4.0, memory=4 * 1024**3, pids_limit=1024)
# Reserve an auxiliary slot for code checks.
_default_budget = (BoundedSemaphore(DEFAULT_EXECUTION["max_containers"] - 1), BoundedSemaphore(1))


def load_execution_settings(configuration):
    supplied = configuration.get('execution', {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_EXECUTION):
        raise ValueError(f'execution accepts {list(DEFAULT_EXECUTION)}')
    settings = {**DEFAULT_EXECUTION, **supplied}
    for key, minimum in (('max_containers', 2), ('pids_limit', 1)):
        if type(settings[key]) is not int or settings[key] < minimum:
            raise ValueError(f'execution.{key} must be an integer >= {minimum}')
    cpus = settings['cpus']
    if type(cpus) not in (int, float) or not math.isfinite(cpus) or cpus < 0.01:
        raise ValueError('execution.cpus must be finite and >= 0.01')
    settings['cpus'] = float(cpus)
    memory = settings['memory']
    if isinstance(memory, str):
        match = re.fullmatch(r'([1-9][0-9]*)([bkmg]?)', memory.strip().lower())
        if not match:
            raise ValueError('execution.memory must be bytes or a size such as 4g or 4096m')
        memory = int(match[1]) * 1024 ** {'': 0, 'b': 0, 'k': 1, 'm': 2, 'g': 3}[match[2]]
    if type(memory) is not int or memory < 6 * 1024**2:
        raise ValueError('execution.memory must be at least 6 MiB')
    settings['memory'] = memory
    return settings


def container_limits(paths):
    labels = container_labels(paths)
    identifier = labels[1].split('=', 1)[1] if labels else None
    return {key: _limits.get(identifier, DEFAULT_EXECUTION)[key] for key in ('cpus', 'memory', 'pids_limit')}


def resource_args(limits):
    # Equal memory and memory-swap limits disable swap beyond the RAM allowance.
    return ['--cpus', str(limits['cpus']), '--memory', str(limits['memory']),
            '--memory-swap', str(limits['memory']), '--pids-limit', str(limits['pids_limit'])]


@contextmanager
def container_slot(paths, *, auxiliary=False):
    labels = container_labels(paths)
    identifier = labels[1].split('=', 1)[1] if labels else None
    stopping = _active.get(identifier)
    main_slots, auxiliary_slots = _budgets.get(identifier, _default_budget)
    slots = auxiliary_slots if auxiliary else main_slots
    while True:
        if stopping is not None and stopping.is_set():
            raise RunCancelled('Run is stopping')
        if slots.acquire(blocking=False):
            break
        # Graders/import checks may use idle runtime capacity. The dedicated
        # auxiliary slot remains unavailable to runtimes, preventing deadlock.
        if auxiliary and main_slots.acquire(blocking=False):
            slots = main_slots
            break
        if slots.acquire(timeout=0.2):
            break
    try:
        if stopping is not None and stopping.is_set():
            raise RunCancelled('Run is stopping')
        yield
    finally:
        slots.release()



class RunCancelled(BaseException):
    """Cancellation must not be counted as an unsuccessful evolutionary round."""


def run_id(directory):
    return hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()


def container_labels(paths, temporary_root=None):
    for path in paths:
        path = Path(path).resolve()
        for directory in (path, *path.parents):
            if (directory / '.run.lock').is_file():
                identifier = run_id(directory)
                # Agent-created lock files must not spoof container ownership or
                # bypass the active run's resource budget and recovery labels.
                if identifier not in _active:
                    continue
                if _active[identifier].is_set():
                    raise RunCancelled('Run is stopping')
                labels = ['--label', f'rsi.run_id={identifier}']
                if temporary_root is not None:
                    labels += ['--label', f'rsi.temp_dir={Path(temporary_root).resolve()}']
                return labels
    return []


def abort_run(paths):
    """Stop new admissions after a container's cleanup cannot be confirmed."""
    try:
        labels = container_labels(paths)
    except RunCancelled:
        return
    if labels:
        event = _active.get(labels[1].split('=', 1)[1])
        if event is not None:
            event.set()


def recover(directory, *, prune=False):
    from pipeline.docker_eval import docker, remove_container
    result = subprocess.run(docker() + ['ps', '-aq', '--filter', f'label=rsi.run_id={run_id(directory)}'],
                            capture_output=True, text=True, timeout=30, check=True)
    names = result.stdout.split()
    temporary = set()
    for name in names:
        info = subprocess.run(docker() + ['inspect', name], capture_output=True, text=True, timeout=30)
        if info.returncode == 0:
            labels = json.loads(info.stdout)[0].get('Config', {}).get('Labels') or {}
            if labels.get('rsi.temp_dir'):
                temporary.add(Path(labels['rsi.temp_dir']))
        remove_container(name)
    if prune:
        for path in temporary:
            if (path.parent == Path(tempfile.gettempdir()).resolve() and not path.is_symlink()
                    and path.name.startswith(('rsi-phase-', 'rsi-meta-', 'rsi-key-'))):
                shutil.rmtree(path, ignore_errors=True)
    return names


@contextmanager
def run_scope(directory, max_containers=3, *, cpus=4.0, memory=4 * 1024**3, pids_limit=1024):
    """Caller holds the directory lock; never remove another live run's containers."""
    settings = load_execution_settings(dict(execution=dict(max_containers=max_containers, cpus=cpus,
        memory=memory, pids_limit=pids_limit)))
    identifier, stopping = run_id(directory), Event()
    _active[identifier] = stopping
    _budgets[identifier] = (BoundedSemaphore(max_containers - 1), BoundedSemaphore(1))
    _limits[identifier] = settings
    previous = {}
    handling_signal = False

    def stop(signum, frame):
        nonlocal handling_signal
        if handling_signal:
            return
        handling_signal = True
        stopping.set()
        try:
            recover(directory)  # Unblock active docker exec/run calls before executor shutdown.
        finally:
            raise KeyboardInterrupt(f'Run interrupted by signal {signum}')

    if current_thread() is main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.signal(signum, stop)
    try:
        recovered = recover(directory, prune=True)
        if recovered:
            from pipeline.storage import log
            log(f'RECOVERY: removed {len(recovered)} containers belonging to this run')
        yield
    finally:
        stopping.set()
        try:
            recover(directory, prune=True)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            _active.pop(identifier, None)
            _budgets.pop(identifier, None)
            _limits.pop(identifier, None)

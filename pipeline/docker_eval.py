"""Small Docker execution boundary for task rollouts and official verification."""
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from pipeline import ROOT
from pipeline.network import POLICY


def check_api_account(result, artifacts, model):
    """Exit 78 is the fixed worker's fatal HTTP 402 signal, not a bad candidate."""
    if result.returncode != 78:
        return
    from pipeline.lifecycle import abort_run, recover, RunCancelled
    from pipeline.storage import log
    message = f'API HTTP 402: insufficient balance; run stopped; details={artifacts}'
    log(message)
    try:
        if 'run_dir' in model:
            abort_run([model['run_dir']])
            recover(Path(model['run_dir']))  # Unblock other active workers before joining them.
    finally:
        raise RunCancelled(message)


@lru_cache(maxsize=1)
def docker():
    if shutil.which("docker"):
        prefixes = [["docker"]] + ([["sudo", "-n", "docker"]] if shutil.which("sudo") else [])
        endpoint = os.environ.get("DOCKER_HOST")
        if endpoint:
            prefixes = [prefix + ["--host", endpoint] for prefix in prefixes]
        for prefix in prefixes:
            try:
                if subprocess.run(prefix + ["info"], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=15).returncode == 0:
                    return prefix
            except subprocess.TimeoutExpired:
                continue
    raise RuntimeError("A running Docker daemon is required for evaluation")



def validate_artifacts(directory):
    # Container-created links/special files must never be dereferenced by host-side readers.
    if directory.is_symlink():
        raise ValueError('Artifact directory must not be a symlink')
    for path in directory.rglob('*'):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f'Unsafe artifact file: {path.name}')


def write_artifact(directory, name, text):
    # Replace the destination itself, rather than following a container-created symlink.
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        temporary.replace(directory / name)
    finally:
        temporary.unlink(missing_ok=True)


def image_name():
    content = (ROOT / "containers/agent.Dockerfile").read_bytes() + (ROOT / "requirements.txt").read_bytes()
    return f"rsi-controller:{hashlib.sha256(content).hexdigest()[:12]}"


def ensure_images():
    client, image = docker(), image_name()
    if subprocess.run(client + ["image", "inspect", image], capture_output=True, timeout=30).returncode == 0:
        return
    with tempfile.TemporaryDirectory(prefix="rsi-build-") as tmp:
        context = Path(tmp)
        shutil.copy2(ROOT / "containers/agent.Dockerfile", context / "Dockerfile")
        shutil.copy2(ROOT / "requirements.txt", context / "requirements.txt")
        subprocess.run(client + ["build", "--network", "host", "-t", image, tmp], check=True, timeout=1800)


def execute(mounts, command, *, cwd="/testbed", timeout=600, env=None, artifacts=None, isolated=False, temporary_root=None):
    from pipeline.lifecycle import container_slot
    from pipeline.network import boundary, mounted_profiles
    with container_slot([host for host, _, _ in mounts], auxiliary=not isolated):
        with boundary(mounted_profiles(mounts)) as (network_args, wrap):
            command = wrap(command)
            return _execute(mounts, command, cwd=cwd, timeout=timeout, env=env,
                            artifacts=artifacts, temporary_root=temporary_root, network_args=network_args)


def _execute(mounts, command, *, cwd="/testbed", timeout=600, env=None, artifacts=None, temporary_root=None, network_args=("--network", "none")):
    from pipeline.lifecycle import container_labels, container_limits, resource_args
    labels = container_labels([host for host, _, _ in mounts], temporary_root)
    limits = container_limits([host for host, _, _ in mounts])
    image = image_name()
    name = "rsi-" + uuid.uuid4().hex
    args = docker() + ["run", "--rm", "--name", name, *network_args,
                       *resource_args(limits),
                       "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp",
                       "--env", "PYTHONDONTWRITEBYTECODE=1", "-w", cwd]
    args += labels
    for host, guest, writable in mounts:
        args += ["--mount", f"type=bind,source={Path(host).resolve()},target={guest}" + ("" if writable else ",readonly")]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    record = dict(container=name, image=image, timeout=timeout, resource_limits=limits,
                  network_policy=POLICY,
                  started_at=datetime.now(timezone.utc).isoformat(), status="running")
    if artifacts is not None:
        artifacts.mkdir(parents=True, exist_ok=True)
        write_artifact(artifacts, "execution.json", json.dumps(record, indent=2))
    result = None
    try:
        result = subprocess.run(args + [image, *command], capture_output=True, text=True, timeout=timeout)
        record.update(status="completed" if result.returncode == 0 else "failed", returncode=result.returncode)
        return result
    except subprocess.TimeoutExpired as exc:
        record["status"] = "timed_out"
        # TimeoutExpired may contain bytes even when text=True.
        decode = lambda value: value.decode(errors="replace") if isinstance(value, bytes) else value or ""
        result = subprocess.CompletedProcess([], None, decode(exc.stdout), decode(exc.stderr))
        raise
    except Exception as exc:
        record.update(status="failed", error_type=type(exc).__name__)
        raise
    except BaseException:
        record["status"] = "interrupted"
        raise
    finally:
        try:
            cleanup = subprocess.run(docker() + ["rm", "-f", name], capture_output=True, text=True, timeout=30)
            record["cleanup_returncode"] = cleanup.returncode
            record["cleanup_status"] = ("removed" if cleanup.returncode == 0 else
                                         "already_removed" if "No such container" in cleanup.stderr else "failed")
        except (OSError, subprocess.SubprocessError) as exc:
            record["cleanup_error"] = type(exc).__name__
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        if artifacts is not None:
            if result is not None:
                write_artifact(artifacts, "stdout.log", result.stdout)
                write_artifact(artifacts, "stderr.log", result.stderr)
            write_artifact(artifacts, "execution.json", json.dumps(record, indent=2))
        if record.get('cleanup_status') == 'failed' or record.get('cleanup_error'):
            from pipeline.lifecycle import abort_run, RunCancelled
            abort_run([host for host, _, _ in mounts])
            raise RunCancelled(f'Container cleanup could not be confirmed; run aborted: {name}')


def remove_container(name):
    result = subprocess.run(docker() + ['rm', '-f', name], capture_output=True, text=True, timeout=30)
    if result.returncode and 'No such container' not in result.stderr:
        raise RuntimeError(f'Container cleanup failed: {name}: {result.stderr[-500:]}')
    return 'removed' if result.returncode == 0 else 'already_removed'


def invoke_meta(workspace, artifacts, model, timeout, **request):
    """Run Meta and its tools together, with only the selected stage evidence."""
    from pipeline.api_config import runtime_profile
    from pipeline.materials import export_view
    artifacts.mkdir(parents=True, exist_ok=True)
    ensure_images()
    runtime = Path(request.pop('runtime_workspace'))
    stage = request['stage']
    if stage not in {'stability_repair', 'forgetting_repair'}:
        raise ValueError(f'Unknown Meta repair stage: {stage}')
    history = Path(request['history_path'])
    if (history.name != stage or history.parent.name != 'views'
            or request['context_paths'] != [str(history / 'index.json')]):
        raise ValueError('Repair must receive exactly its scheduler-owned stage view')
    validate_artifacts(history)
    if json.loads((history / 'index.json').read_text()).get('stage') != stage:
        raise ValueError('Repair view stage differs from the requested stage')
    settings = runtime_profile(model, 'meta_agent')
    with tempfile.TemporaryDirectory(prefix='rsi-meta-') as tmp:
        context = Path(tmp)
        materials, candidate, engine = context/'materials', context/'candidate', context/'engine'
        export_view(history, materials)
        shutil.copytree(workspace/'harness/task', candidate)
        shutil.copytree(runtime/'harness/meta', engine/'harness/meta',
                        ignore=shutil.ignore_patterns('__pycache__'))
        secret = context/'api.yaml'
        secret.write_text(json.dumps(dict(task_agent=dict(model='unused', api_key='unused'),
                                          meta_agent=settings.runtime())))
        secret.chmod(0o600)
        # No host paths, remaining-round metadata or sibling snapshot enters the request.
        guest_request = dict(kind='meta', stage=stage, api_role='meta_agent', workspace='/candidate',
                             candidate_path='/candidate/harness/task', runtime_workspace='/engine',
                             model=dict(config_path='/run/secrets/api.yaml'), history_path='/materials',
                             context_paths=['/materials/index.json'], instruction=None,
                             log_path='/output/chat.log', result_path='/output/result.json')
        write_artifact(artifacts, 'request.json', json.dumps(guest_request, indent=2))
        try:
            mounts = [(artifacts, '/output', True),
                      (candidate, '/candidate/harness/task', True), (materials, '/materials', False),
                      (engine/'harness/meta', '/engine/harness/meta', False),
                      (ROOT/'pipeline/worker.py', '/runner/worker.py', False),
                      (ROOT/'pipeline/api_config.py', '/runner/api_config.py', False),
                      (secret, '/run/secrets/api.yaml', False)]
            result = execute(mounts, ['python', '/runner/worker.py', '/output/request.json'],
                             cwd='/candidate', timeout=timeout, artifacts=artifacts, isolated=True,
                             temporary_root=context)
        finally:
            # Export only Task code; runtime and evidence mounts are read-only.
            validate_artifacts(candidate)
            shutil.rmtree(workspace/'harness/task')
            shutil.copytree(candidate, workspace/'harness/task')
    check_api_account(result, artifacts, model)
    if result.returncode:
        raise RuntimeError(f'Meta failed: {artifacts / "stderr.log"}')
    validate_artifacts(artifacts)
    return json.loads((artifacts/'result.json').read_text())

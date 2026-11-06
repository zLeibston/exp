"""Fresh Harbor task containers, hidden post-run tests, existing worker and run budgets.

Task state (including services) survives until grading. No Docker socket, source
pool, reference solution or verifier is exposed during the agent phase.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Lock
import tomllib
import uuid

from pipeline import ROOT
from pipeline.api_config import runtime_profile
from pipeline.docker_eval import docker, remove_container, validate_artifacts, write_artifact, check_api_account
from pipeline.lifecycle import container_slot, container_labels, container_limits, resource_args, abort_run, RunCancelled
from pipeline.network import boundary, POLICY
from pipeline.storage import write

_BUILD_LOCK = Lock()
# Bootstrap independently of task pip configuration (some tasks intentionally
# break pip). The temporary installer is removed; task files remain untouched.
DEPENDENCIES = 'httpx==0.28.1 PyYAML==6.0.3'
RUNNER_LAYER = '''
USER root
RUN if ! python3 -m venv /opt/rsi-bootstrap; then \\
      apt-get update && apt-get install -y python3 python3-venv && \\
      python3 -m venv /opt/rsi-bootstrap; fi
RUN /opt/rsi-bootstrap/bin/python -c "import os, subprocess; \\
    env = {k:v for k,v in os.environ.items() if not k.startswith('PIP_')}; \\
    env['PIP_CONFIG_FILE'] = '/dev/null'; \\
    subprocess.run(['/opt/rsi-bootstrap/bin/python', '-m', 'pip', 'install', \\
    '--no-cache-dir', '--index-url', 'https://pypi.org/simple', \\
    '--target', '/opt/rsi-deps', *'DEPENDENCIES'.split()], env=env, check=True)" && \\
    rm -rf /opt/rsi-bootstrap
'''.replace('DEPENDENCIES', DEPENDENCIES)

TASK_NETWORK_POLICY = 'terminal-task-config-v1'


def allows_internet(configuration):
    """Harbor's legacy task format defaults to public network access."""
    environment = configuration.get('environment', {})
    if 'network_mode' in environment:
        mode = environment['network_mode']
        if mode not in {'public', 'no-network'}:
            raise ValueError(f'Unsupported terminal network_mode: {mode!r}')
        return mode == 'public'
    value = environment.get('allow_internet', True)
    if type(value) is not bool:
        raise ValueError('Terminal allow_internet must be a boolean')
    return value


def image_tags(case):
    version = hashlib.sha256((case['data_hash'] + RUNNER_LAYER).encode()).hexdigest()[:20]
    return f'rsi-terminal-task:{version}', f'rsi-terminal-agent:{version}'


def setup_environment(cases):
    # Build on demand under the run's admission budget, rather than building the
    # whole 200-task pool before --limit or a scheduled subset can take effect.
    return {case['id']: dict(task=image_tags(case)[0], agent=image_tags(case)[1],
                            data_hash=case['data_hash']) for case in cases}


def prepare(case, directory):
    directory.mkdir(parents=True, exist_ok=True)
    return dict(case['inputs'], _task_path=case['task_path'], _data_hash=case['data_hash'])


def ensure_image(task_path, data_hash, expected_id=None, log_path=None):
    from tasks.terminal.loader import task_digest
    if task_digest(task_path) != data_hash:
        raise ValueError(f'Terminal task changed after run setup: {task_path.name}')
    base, agent = image_tags(dict(data_hash=data_hash))
    with _BUILD_LOCK:
        if subprocess.run(docker() + ['image', 'inspect', agent], capture_output=True, timeout=30).returncode == 0:
            return agent
        if expected_id is not None:
            raise RuntimeError(f'Previously used terminal image is missing: {agent}; restore {expected_id} before resuming')
        configuration = tomllib.loads((task_path / 'task.toml').read_text())
        timeout = positive_seconds(configuration.get('environment', {}).get('build_timeout_sec', 1800))
        with ExitStack() as resources:
            log = resources.enter_context(Path(log_path).open('w')) if log_path else None
            subprocess.run(docker() + ['build', '--network', 'host', '-t', base, str(task_path / 'environment')],
                           check=True, timeout=timeout, stdout=log, stderr=subprocess.STDOUT if log else None)
            tmp = resources.enter_context(tempfile.TemporaryDirectory(prefix='rsi-terminal-build-'))
            (Path(tmp) / 'Dockerfile').write_text(f'FROM {base}\n' + RUNNER_LAYER)
            subprocess.run(docker() + ['build', '--network', 'host', '-t', agent, tmp],
                           check=True, timeout=timeout, stdout=log, stderr=subprocess.STDOUT if log else None)
    return agent


def positive_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError('Task timeout must be positive and finite')
    return value


def limits_for(configuration, paths):
    limits = container_limits(paths)
    env = configuration.get('environment', {})
    cpu = positive_seconds(env.get('cpus', limits['cpus']))
    memory = env.get('memory_mb', limits['memory'] // 1024**2)
    if type(memory) is not int or memory < 6:
        raise ValueError('Terminal task memory_mb must be an integer >= 6')
    return dict(limits, cpus=min(cpu, limits['cpus']), memory=min(memory * 1024**2, limits['memory']))


def read_reward(text):
    # A missing/invalid reward is an evaluator error, never an ordinary task miss.
    value = text.strip()
    if value not in {'0', '1'}:
        raise ValueError(f'Invalid or missing terminal verifier reward: {value[:100]!r}')
    return value == '1'


def exec_in(name, command, *, timeout, cwd=None, env=None):
    args = docker() + ['exec']
    if cwd is not None:
        args += ['-w', cwd]
    for key, value in (env or {}).items():
        args += ['-e', f'{key}={value}']
    return subprocess.run(args + [name, *command], capture_output=True, text=True, timeout=timeout)


def invoke_task(workspace, directory, model, timeout, inputs):
    task_path = Path(inputs['_task_path'])
    configuration = tomllib.loads((task_path / 'task.toml').read_text())
    internet = allows_internet(configuration)
    agent_timeout = min(timeout, positive_seconds(configuration.get('agent', {}).get('timeout_sec', timeout)))
    verifier_timeout = positive_seconds(configuration.get('verifier', {}).get('timeout_sec', 360))
    directory.mkdir(parents=True, exist_ok=True)
    verifier = directory / 'verifier'
    verifier.mkdir()
    name = 'rsi-terminal-' + uuid.uuid4().hex
    record = dict(container=name, task_id=task_path.name,
                  network_policy='public' if internet else POLICY,
                  task_network_policy=TASK_NETWORK_POLICY, allow_internet=internet,
                  source_harness=str(workspace), data_hash=inputs['_data_hash'],
                  started_at=datetime.now(timezone.utc).isoformat(), status='starting')
    result = None
    def failed(response):
        write(verifier / 'result.json', dict(passed=False, status='agent_failed', error=None,
                                            task_failure=response['task_failure']))
        record.update(status='agent_failed', passed=False, task_failure=response['task_failure'])
        return response
    # The same admission covers both solving and grading. No nested slot is
    # acquired, so a full task queue cannot deadlock waiting for graders.
    with ExitStack() as resources:
        resources.enter_context(container_slot([directory]))
        pin = (Path(model['run_dir']) / 'image_pins' / (image_tags(dict(data_hash=inputs['_data_hash']))[1].split(':')[1] + '.json')
               if 'run_dir' in model else None)
        previous_id = json.loads(pin.read_text())['image_id'] if pin is not None and pin.exists() else None
        image = ensure_image(task_path, inputs['_data_hash'], previous_id, directory / 'build.log')
        record.update(image=image, resource_limits=limits_for(configuration, [directory]))
        temporary = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix='rsi-phase-')))
        output = temporary / 'output'
        output.mkdir()
        config_path = temporary / 'api.yaml'
        settings = runtime_profile(model, 'task_agent')
        config_path.write_text(json.dumps(dict(task_agent=settings.runtime(),
                                              meta_agent=dict(model='unused', api_key='unused'))))
        config_path.chmod(0o600)
        # These legacy tasks grade in the same environment as the agent.
        # Respect the dataset's network policy in both phases.
        if internet:
            network_args, wrap = ['--network', 'bridge'], lambda command, **kwargs: command
        else:
            network_args, wrap = resources.enter_context(boundary([settings]))
        info = subprocess.run(docker() + ['image', 'inspect', image], check=True,
                              capture_output=True, text=True, timeout=30)
        image_config = json.loads(info.stdout)[0]
        cwd = image_config.get('Config', {}).get('WorkingDir') or '/'
        record['image_id'] = image_config['Id']
        if pin is not None:
            with _BUILD_LOCK:
                if pin.exists():
                    if json.loads(pin.read_text())['image_id'] != record['image_id']:
                        raise ValueError('Terminal image identity changed; cached results require the original image')
                else:
                    write(pin, dict(image=image, image_id=record['image_id']))
        mounts = [(Path(workspace) / 'harness/task', '/engine/harness/task', False),
                  (ROOT / 'pipeline/worker.py', '/runner/worker.py', False),
                  (ROOT / 'pipeline/api_config.py', '/runner/api_config.py', False),
                  (config_path, '/run/secrets/api.yaml', False), (output, '/output', True)]
        forwarded = {key: value for key, value in inputs.items() if not key.startswith('_')}
        forwarded.update(working_directory=cwd, allow_internet=internet)
        request = dict(kind='task', workspace='/engine', runtime_workspace='/engine',
                       model=dict(config_path='/run/secrets/api.yaml'), inputs=forwarded,
                       log_path='/output/chat.log', result_path='/output/result.json')
        (output / 'request.json').write_text(json.dumps(request, ensure_ascii=False, indent=2))
        args = docker() + ['run', '-d', '--name', name, '--user', '0:0', '--init',
                          *network_args, *resource_args(record['resource_limits']),
                          *container_labels([directory], temporary),
                          '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=/opt/rsi-deps:/engine:/runner',
                          '-e', 'NO_PROXY=localhost,127.0.0.1', '-e', 'no_proxy=localhost,127.0.0.1']
        for host, guest, writable in mounts:
            args += ['--mount', f'type=bind,source={host.resolve()},target={guest}' + ('' if writable else ',readonly')]
        write(directory / 'container.json', record)
        try:
            subprocess.run(args + ['--entrypoint', '', image, *wrap(['sleep', 'infinity'], python='python3')],
                           check=True, capture_output=True, text=True, timeout=60)
            if configuration.get('startup_required'):
                if not (task_path / 'start.sh').is_file():
                    raise ValueError('Task requires a missing start.sh')
                subprocess.run(docker() + ['cp', str(task_path / 'start.sh'), name + ':/tmp/rsi-start.sh'],
                               check=True, capture_output=True, timeout=30)
                startup = exec_in(name, ['bash', '/tmp/rsi-start.sh'], timeout=agent_timeout, cwd=cwd)
                write_artifact(directory, 'startup.log', startup.stdout + startup.stderr)
                if startup.returncode:
                    raise RuntimeError('Terminal environment startup failed')
            # Successful workers may leave services running: grading needs them.
            # On timeout/cancellation the entire private container is removed.
            record['phase'] = 'agent'
            result = exec_in(name, ['python3', '/runner/worker.py', '/output/request.json'],
                             timeout=agent_timeout, cwd=cwd)
            write_artifact(directory, 'stdout.log', result.stdout)
            write_artifact(directory, 'stderr.log', result.stderr)
            check_api_account(result, directory, model)
            if result.returncode:
                raise RuntimeError(f'Terminal task worker failed: {directory / "stderr.log"}')
            validate_artifacts(output)
            response = json.loads((output / 'result.json').read_text())
            if response.get('task_failure'):
                return failed(response)
            # Tests only enter the container after the agent worker has finished.
            reset = exec_in(name, ['sh', '-c', 'rm -rf /tests /logs/verifier; mkdir -p /tests /logs/verifier'], timeout=30)
            if reset.returncode:
                raise RuntimeError('Could not prepare terminal verifier directories')
            subprocess.run(docker() + ['cp', str(task_path / 'tests') + '/.', name + ':/tests'],
                           check=True, capture_output=True, text=True, timeout=60)
            record['phase'] = 'verifier'
            grading = exec_in(name, ['bash', '/tests/test.sh'], timeout=verifier_timeout, cwd=cwd,
                              env=configuration.get('verifier', {}).get('env', {}))
            write_artifact(directory, 'verifier.log', grading.stdout + grading.stderr)
            reward = exec_in(name, ['cat', '/logs/verifier/reward.txt'], timeout=30)
            if reward.returncode:
                raise ValueError('Terminal verifier produced no reward.txt; inspect verifier.log')
            passed = read_reward(reward.stdout)
            # Nonzero shell status plus reward=1 is contradictory evidence.
            if grading.returncode and passed:
                raise ValueError('Terminal verifier failed but reported success')
            write(verifier / 'result.json', dict(passed=passed, status='resolved' if passed else 'unresolved',
                                                verifier_exit_code=grading.returncode, error=None))
            record.update(status='completed', passed=passed)
            return response
        except subprocess.TimeoutExpired as exc:
            record.update(status='timed_out')
            decode = lambda text: text.decode(errors='replace') if isinstance(text, bytes) else text or ''
            if record.get('phase') == 'verifier':
                write_artifact(directory, 'verifier.log', decode(exc.stdout) + decode(exc.stderr))
            else:
                write_artifact(directory, 'stdout.log', decode(exc.stdout))
                write_artifact(directory, 'stderr.log', decode(exc.stderr))
            if record.get('phase') == 'agent':
                # docker exec timing out does not stop the guest process. Stop it
                # before reading its journal; never grade a still-running agent.
                remove_container(name)
                validate_artifacts(output)
                from pipeline.worker import failed_task_result
                response = failed_task_result(output, 'agent_timeout', type(exc).__name__)
                write(output / 'result.json', response)
                return failed(response)
            raise
        except BaseException as exc:
            record.update(status='failed', error_type=type(exc).__name__)
            raise
        finally:
            # Export conversations only, never /tests, task solutions, or the
            # whole filesystem (which could contain verifier answers).
            try:
                validate_artifacts(output)
                shutil.copytree(output, directory, dirs_exist_ok=True)
                # A killed worker cannot close its trace; retain partial history
                # and explicitly distinguish interruption from a completed run.
                execution = directory/'execution.json'
                if execution.is_file():
                    summary = json.loads(execution.read_text())
                    if summary.get('status') == 'running':
                        summary.update(status='interrupted', stop_reason=record.get('task_failure', {}).get(
                            'reason', record.get('status', 'worker_stopped')))
                        write(execution, summary)
            finally:
                try:
                    record['cleanup_status'] = remove_container(name)
                except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                    record['cleanup_status'] = 'failed'
                    abort_run([directory])
                    raise RunCancelled(f'Terminal container cleanup failed; stopping run: {name}') from exc
                finally:
                    record['finished_at'] = datetime.now(timezone.utc).isoformat()
                    write(directory / 'container.json', record)


def grade(case, prediction, directory):
    return json.loads((directory / 'verifier/result.json').read_text())

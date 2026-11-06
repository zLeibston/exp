"""Trusted run setup and resume for the repeated-evaluation A/B/C scheduler."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from pipeline import ROOT
from pipeline.network import POLICY as network_policy
from pipeline.evaluation import load_evaluation_settings
from pipeline.storage import run_lock, write, snapshot, harness_digest, log
from pipeline.docker_eval import docker, ensure_images, image_name
from tasks import load_domain


def run(output=None, rounds=1, timeout=600, config=None, *, domain="terminal",
        split='train', limit=None, dataset=None, eval_only=False, harness=None, resume=False):
    from pipeline.api_config import load_config
    from pipeline.evolution import LAYOUT, evolve, validate_state
    from pipeline.execution_pool import ExecutionPool
    from pipeline.lifecycle import load_execution_settings, run_scope
    from pipeline.scheduling import load_settings
    import yaml

    if resume and (output is None or eval_only or harness):
        raise ValueError('Resume requires an existing output and cannot use eval-only or a seed harness')
    if type(rounds) is not int or rounds < 1 or timeout <= 0:
        raise ValueError('rounds and timeout must be positive')
    if not eval_only and split != 'train':
        raise ValueError('Validation/test splits are evaluation-only; use --eval-only')
    suite = load_domain(domain, split=split, limit=limit, dataset=dataset)
    suite.split = split
    config = Path(config or ROOT / 'config.yaml').resolve()
    configuration = yaml.safe_load(config.read_text())
    if not isinstance(configuration, dict):
        raise ValueError('Configuration must be a YAML mapping')
    scheduler = load_settings(config)
    execution = load_execution_settings(configuration)
    evaluation = load_evaluation_settings(config)
    # Historical trajectory evidence is an input to this algorithm and is never pruned.
    roles = ['task_agent'] if eval_only else ['task_agent', 'meta_agent']
    profiles = load_config(config, roles=roles)
    model = dict(config_path=str(config), profiles=profiles)
    output = (Path(output).resolve() if output is not None else
              ROOT / 'runs' / f'{domain}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S-%f}')
    # Reject incompatible resumes before building images or touching old evidence.
    previous = None
    if resume:
        previous = json.loads((output / 'config.json').read_text())
        if previous.get('layout') != LAYOUT:
            raise ValueError('This run uses an incompatible pipeline layout; start a fresh A/B/C RSI run')
    task_images = suite.setup()
    docker()
    ensure_images()
    images = dict(controller=dict(tag=image_name(), image_id=subprocess.check_output(
        docker() + ['image', 'inspect', '--format', '{{.Id}}', image_name()], text=True).strip()))
    images.update(task_images)
    settings = dict(layout=LAYOUT, network_policy=network_policy, task_network_policy=suite.network_policy,
                    domain=domain, dataset=dataset,
                    rounds=rounds, timeout=timeout, suite=suite.name, split=split, cases=suite.cases,
                    agents={role: cfg.public() for role, cfg in profiles.items()}, images=images,
                    scheduler=scheduler, evaluation=evaluation, execution=execution,
                    mode='eval_only' if eval_only else 'evolution')
    with run_lock(output, resume), run_scope(output, **execution):
        state = None
        initial = output / 'initial'
        if resume:
            previous = json.loads((output / 'config.json').read_text())
            for key in ('layout', 'mode', 'network_policy', 'task_network_policy', 'timeout', 'suite', 'split',
                        'cases', 'agents', 'images', 'scheduler', 'domain', 'dataset'):
                if previous.get(key) != settings[key]:
                    raise ValueError(f'Resume configuration differs: {key}; start a fresh run')
            for key in ('cpus', 'memory', 'pids_limit'):
                if previous['execution'][key] != execution[key]:
                    raise ValueError(f'Resume container limit differs: {key}')
            if harness_digest(initial) != previous['initial_harness_hash']:
                raise ValueError('Initial harness snapshot changed')
            if (output / 'state.json').exists():
                state = json.loads((output / 'state.json').read_text())
                validate_state(state, initial)
            write(output / 'resume.json', settings)
        else:
            snapshot(Path(harness).resolve() if harness else ROOT, initial)
            settings['initial_harness_hash'] = harness_digest(initial)
            write(output / 'config.json', settings)
        log(f'Run directory: {output}')
        log(f'A/B/C RSI: pool={len(suite.cases)}, cold_start={scheduler["cold_start_repeats"]} repeats, '
            f'round_repeats={scheduler["repeats_per_task"]}, quotas={scheduler["quotas"]}, workers={evaluation["workers"]}')
        if eval_only:
            # Standalone benchmark evaluation is one pass, with no labels or evolution.
            with ExecutionPool(model, evaluation['workers']) as pool:
                result = pool.evaluate(initial, output / 'evaluation', model, timeout, suite)
            write(output / 'result.json', result)
            return result
        return evolve(output, initial, suite, model, timeout, rounds, state, scheduler, evaluation['workers'])

"""Repeated-evaluation RSI: classify, repair stability, then repair forgetting.

Each changed candidate is accepted only after improvement on its stage's tasks.
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

from pipeline.editing import check, propose
from pipeline.execution_pool import ExecutionPool
from pipeline.materials import build_view
from pipeline.scheduling import (STAGES, label_counts, repeated_evaluation,
                                 select_tasks, update_statistics)
from pipeline.storage import harness_digest, log, write

LAYOUT = 'rsi-repeated-abc-v4'


def validate_state(state, initial):
    """Every recorded version and every task's evidence remains immutable."""
    versions = {str(initial): state['initial_harness_hash']}
    for generation in state['generations']:
        for version in generation['versions']:
            previous = versions.setdefault(version['workspace'], version['harness_hash'])
            if previous != version['harness_hash']:
                raise ValueError('Conflicting committed harness identity')
    for workspace, identity in versions.items():
        if harness_digest(Path(workspace)) != identity:
            raise ValueError(f'Committed harness changed: {workspace}')
    for entry in state['tasks'].values():
        observation = entry['last_observation']
        if versions.get(observation['workspace']) != observation['harness_hash']:
            raise ValueError('Task evidence references an uncommitted harness')
        for trial in observation['attempts']:
            if not Path(trial['trajectory']).is_dir():
                raise ValueError(f'Missing task trajectory: {trial["trajectory"]}')


def repair(source, phase, view, model, timeout, stage, remaining):
    """A checked phase receipt permits safe resume without repeating Meta calls."""
    receipt = phase / 'completed.json'
    source_hash = harness_digest(source)
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        candidate = Path(saved['workspace'])
        if saved['source_hash'] != source_hash or saved['harness_hash'] != harness_digest(candidate):
            raise ValueError(f'Repair checkpoint changed: {phase}')
        return candidate
    phase.mkdir(parents=True, exist_ok=True)
    # An interrupted/unvalidated edit is evidence, not a publishable version.
    # Preserve it and retry in a new directory on resume.
    number = 1 + max([int(p.name[8:]) for p in phase.glob('attempt_*') if p.is_dir()] or [0])
    attempt = phase / f'attempt_{number:04d}'
    candidate = attempt / 'version'
    propose(source, candidate, attempt / 'edit', model, timeout, stage,
            [view / 'index.json'], view, remaining)
    write(receipt, dict(workspace=str(candidate), source_hash=source_hash,
                        harness_hash=harness_digest(candidate), stage=stage))
    return candidate


def evolve(output, initial, suite, model, timeout, rounds, state, settings, workers):
    if state is None:
        check(initial, timeout, output / 'initial_contract.log')
        current, statistics = initial, {}
        first = 1
    else:
        validate_state(state, initial)
        current = Path(state['current_workspace'])
        statistics = deepcopy(state['tasks'])
        first = state['round'] + 1
    for index in range(first, rounds + 1):
        cold = index == 1
        folder = output / f'round_{index:04d}'
        folder.mkdir(exist_ok=True)
        selected, counts = ((suite.cases, {}) if cold else select_tasks(suite.cases, statistics, settings['quotas']))
        repeats = settings['cold_start_repeats'] if cold else settings['repeats_per_task']
        identity = harness_digest(current)
        plan = dict(round=index, cold_start=cold, evaluated_workspace=str(current), evaluated_harness_hash=identity,
                    selected=[case['id'] for case in selected], quotas=settings['quotas'],
                    actual_quotas=counts, repeats_per_task=repeats)
        if (folder / 'plan.json').exists() and json.loads((folder / 'plan.json').read_text()) != plan:
            raise ValueError('Pending round plan differs from committed scheduler state')
        write(folder / 'plan.json', plan)
        log(f'ROUND {index}/{rounds}: cold_start={cold}, tasks={len(selected)}, repeats={repeats}, quotas={counts}')
        if not cold and sum(counts.values()) < sum(settings['quotas'].values()):
            log('CLASS SHORTAGE: taking available tasks only; no replacement or cross-class filling')
        observations = {}
        if selected:
            with ExecutionPool(model, workers) as pool:
                observations = repeated_evaluation(pool, current, folder / 'evaluation', model, timeout,
                                                   replace(suite, cases=selected), repeats)
        updated, transitions = update_statistics(statistics, selected, observations, current, identity, index, repeats)
        # Persist label updates before either Meta stage. state.json below remains
        # the atomic round commit; incomplete rounds replay these same trial receipts.
        write(folder / 'labels.json', dict(tasks=updated, transitions=transitions, counts=label_counts(updated)))
        versions = [dict(workspace=str(current), harness_hash=identity, stage='evaluated')]
        result = current
        stages = []
        validation_count = validation_correct = validation_errors = 0
        for stage in STAGES:
            affected = [entry['task_id'] for entry in transitions if entry['stage'] == stage]
            if cold or not affected:
                stages.append(dict(stage=stage, status='skipped', tasks=affected))
                continue
            log(f'ROUND {index}: {stage}, tasks={affected}')
            view = build_view(folder / 'views' / stage, stage, selected, transitions, current, result)
            candidate = repair(result, folder / stage, view, model, timeout, stage, rounds-index)
            candidate_hash = harness_digest(candidate)
            if candidate_hash == harness_digest(result):
                stages.append(dict(stage=stage, status='unchanged', tasks=affected))
                continue
            validation_cases = [case for case in selected if case['id'] in affected]
            validation_repeats = settings['repeats_per_task']
            with ExecutionPool(model, workers) as pool:
                validation = repeated_evaluation(pool, candidate, folder / stage / 'validation', model, timeout,
                                                 replace(suite, cases=validation_cases), validation_repeats)
            before = sum(entry['correct'] for entry in transitions if entry['stage'] == stage)
            after = sum(trial['passed'] for trials in validation.values() for trial in trials
                        if trial['error'] is None)
            accepted = after > before
            updated, _ = update_statistics(updated, validation_cases, validation, candidate,
                                           candidate_hash, index, validation_repeats,
                                           accept_observation=accepted)
            version = dict(workspace=str(candidate), harness_hash=candidate_hash, stage=stage)
            versions.append(version)
            decision = dict(status='accepted' if accepted else 'rejected', tasks=affected,
                            before_correct=before, after_correct=after,
                            evaluations=len(validation_cases)*validation_repeats, **version)
            write(folder / stage / 'validation.json', decision)
            stages.append(decision)
            validation_count += decision['evaluations']
            validation_correct += after
            validation_errors += sum(trial['error'] is not None for trials in validation.values() for trial in trials)
            if accepted:
                result = candidate
            log(f'ROUND {index}: {stage} {decision["status"]}, correct={before}->{after}')
        write(folder / 'labels.json', dict(tasks=updated, transitions=transitions, counts=label_counts(updated)))
        summary = dict(round=index, cold_start=cold, evaluated_workspace=str(current),
                       current_workspace=str(result), versions=versions, stages=stages,
                       label_counts=label_counts(updated), selected=plan['selected'],
                       valid_evaluations=len(selected)*repeats + validation_count,
                       correct=sum(entry['correct'] for entry in transitions) + validation_correct,
                       validation_evaluations=validation_count, validation_correct=validation_correct,
                       errors=sum(trial['error'] is not None for trials in observations.values() for trial in trials)
                              + validation_errors)
        write(folder / 'summary.json', summary)
        generations = [*(state['generations'] if state else []), summary]
        state = dict(layout=LAYOUT, round=index, initial_harness_hash=harness_digest(initial),
                     current_workspace=str(result), previous_workspace=str(current),
                     harness_hash=harness_digest(result), tasks=updated, generations=generations)
        write(output / 'state.json', state)
        log(f'ROUND {index}: committed, labels={summary["label_counts"]}, harness={result}')
        statistics, current = updated, result
    return state

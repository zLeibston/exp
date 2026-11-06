"""Trusted A/B/C bookkeeping, repeated-trial receipts and bounded Meta evidence."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from pipeline.storage import write

DEFAULTS = dict(cold_start_repeats=4, repeats_per_task=4, quotas=dict(A=5, B=10, C=5))
STAGES = ('stability_repair', 'forgetting_repair')


def load_settings(path):
    import yaml
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError('Configuration must be a YAML mapping')
    supplied = data.get('scheduler', {})
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
        raise ValueError(f'scheduler accepts {list(DEFAULTS)}')
    settings = {**deepcopy(DEFAULTS), **supplied}
    for key in ('cold_start_repeats', 'repeats_per_task'):
        if type(settings[key]) is not int or settings[key] < 2:
            raise ValueError(f'scheduler.{key} must be an integer >= 2')
    quotas = settings['quotas']
    if (not isinstance(quotas, dict) or set(quotas) != {'A', 'B', 'C'}
            or any(type(v) is not int or v < 0 for v in quotas.values()) or sum(quotas.values()) == 0):
        raise ValueError('scheduler.quotas requires nonnegative integer A/B/C counts with positive total')
    return settings


def classify(correct, evaluations):
    if type(correct) is not int or type(evaluations) is not int or evaluations < 1 or not 0 <= correct <= evaluations:
        raise ValueError('Invalid repeated evaluation counts')
    return 'A' if correct == evaluations else 'C' if correct == 0 else 'B'


def repair_stage(before, after):
    if (before == 'A' and after in {'B', 'C'}) or (before == 'B' and after == 'C'):
        return 'forgetting_repair'
    if before == after == 'B':
        return 'stability_repair'
    return None


def select_tasks(cases, statistics, quotas):
    """No replacement, no cross-class filling; equal counts break ties by task ID."""
    chosen, counts = [], {}
    for label in ('A', 'B', 'C'):
        bucket = [case for case in cases if statistics[case['id']]['label'] == label]
        bucket.sort(key=lambda case: (statistics[case['id']]['evaluation_count'], case['id']))
        chosen.extend(bucket[:quotas[label]])
        counts[label] = min(len(bucket), quotas[label])
    return chosen, counts


def repeated_evaluation(pool, workspace, folder, model, timeout, suite, repeats):
    """Queue all missing (task, repeat) trials together, with no repeat barriers.

    Durable receipts keep valid work reusable after interruption. Failed trials
    are retained and retried only on resume; labels require all valid trials.
    """
    from pipeline.evaluation import save_report
    folder.mkdir(parents=True, exist_ok=True)
    observations = {case['id']: [] for case in suite.cases}
    expected = {case['id']: case for case in suite.cases}
    jobs, groups = [], {}

    def record(item, repetition, directory):
        identifier = item['task']['id']
        if expected.get(identifier) != item['task'] or type(item['passed']) is not bool:
            raise ValueError('Cached trial differs from the selected task contract')
        trial = dict(repeat=repetition, passed=item['passed'], error=item.get('error'),
                     trajectory=str(directory / identifier))
        observations[identifier].append(trial)
        return identifier, trial

    for repetition in range(1, repeats + 1):
        repeat_dir = folder / f'repeat_{repetition:02d}'
        repeat_dir.mkdir(exist_ok=True)
        successful = set()
        for attempt_dir in sorted(repeat_dir.glob('attempt_*')):
            path = attempt_dir / 'summary.json'
            items = (json.loads(path.read_text())['cases'] if path.exists() else
                     [json.loads(p.read_text()) for p in sorted(attempt_dir.glob('*/evaluation.json'))])
            for item in items:
                identifier, trial = record(item, repetition, attempt_dir)
                if trial['error'] is None:
                    if identifier in successful:
                        raise ValueError('Duplicate completed trial in repeated evaluation cache')
                    successful.add(identifier)
        missing = [case for case in suite.cases if case['id'] not in successful]
        if missing:
            number = 1 + max([int(p.name[8:]) for p in repeat_dir.glob('attempt_*') if p.is_dir()] or [0])
            directory = repeat_dir / f'attempt_{number:04d}'
            groups[directory] = []
            jobs.extend((case, directory / case['id']) for case in missing)

    if jobs:
        # One shared executor receives all repetitions; there is no nested pool
        # and no wait for one repetition before the next can begin.
        results = pool.evaluate_trials(workspace, jobs, model, timeout, suite)
        if len(results) != len(jobs):
            raise ValueError('Trial executor returned an incomplete result list')
        for (case, directory), item in zip(jobs, results):
            if item['task'] != case:
                raise ValueError('Trial executor changed job/result ordering')
            repetition = int(directory.parent.parent.name[7:])
            record(item, repetition, directory.parent)
            groups[directory.parent].append(item)
        for directory, items in groups.items():
            save_report(suite, items, directory)

    errors = []
    for identifier, trials in observations.items():
        trials.sort(key=lambda trial: (trial['repeat'], trial['trajectory']))
        valid = {trial['repeat'] for trial in trials if trial['error'] is None}
        if valid != set(range(1, repeats + 1)):
            errors.append(identifier)
    if errors:
        write(folder / 'errors.json', dict(tasks=errors,
              note='Execution errors are not incorrect answers. Resume retries only missing/failed trials.'))
        raise RuntimeError(f'Repeated evaluation has execution errors: {errors}; inspect {folder}; resume after fixing the environment/API')
    (folder / 'errors.json').unlink(missing_ok=True)
    return observations


def update_statistics(statistics, cases, observations, workspace, harness_hash, round_index, repeats):
    updated = deepcopy(statistics)
    transitions = []
    for case in cases:
        identifier = case['id']
        attempts = observations[identifier]
        valid = [trial for trial in attempts if trial['error'] is None]
        if len(valid) != repeats:
            raise ValueError('A label requires every repeated evaluation to complete')
        correct = sum(trial['passed'] for trial in valid)
        previous = deepcopy(updated.get(identifier))
        current = dict(label=classify(correct, repeats), evaluation_count=len(attempts),
                       correct_count=correct, valid_evaluation_count=repeats, error_count=len(attempts)-repeats,
                       last_evaluated_round=round_index)
        if previous:
            for key in ('evaluation_count', 'correct_count', 'valid_evaluation_count', 'error_count'):
                current[key] += previous[key]
        observation = dict(round=round_index, workspace=str(workspace), harness_hash=harness_hash,
                           correct=correct, evaluations=repeats, attempts=attempts)
        current['last_observation'] = observation
        updated[identifier] = current
        transitions.append(dict(task_id=identifier, before=previous['label'] if previous else None,
                                after=current['label'], correct=correct, evaluations=repeats,
                                stage=repair_stage(previous['label'], current['label']) if previous else None,
                                previous=previous['last_observation'] if previous else None,
                                current=observation))
    return updated, transitions


def label_counts(statistics):
    return dict(Counter(entry['label'] for entry in statistics.values()))

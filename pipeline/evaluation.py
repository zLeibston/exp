"""Domain-neutral trial execution and one shared queue across every repetition."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import subprocess

from pipeline.storage import log, report_index, write

TRIAL_ERRORS = (RuntimeError, subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError)

def load_evaluation_settings(path):
    import yaml
    from pathlib import Path
    data = yaml.safe_load(Path(path).read_text())
    supplied = data.get('evaluation', {})
    if not isinstance(supplied, dict) or set(supplied) - {'workers'}:
        raise ValueError('evaluation accepts workers')
    workers = supplied.get('workers', 50)
    if type(workers) is not int or workers < 1:
        raise ValueError('evaluation.workers must be a positive integer')
    return dict(workers=workers)


def evaluate_case(workspace, directory, model, timeout, suite, case, task_runner=None, preparation_error=None):
    """The receipt is durable before the worker takes another job."""
    directory.mkdir(parents=True, exist_ok=True)
    prediction = None
    try:
        if preparation_error is not None:
            raise preparation_error
        inputs = suite.prepare(case, directory)
        response = (task_runner or suite.run)(workspace, directory, model, timeout, inputs)
        prediction = response['prediction']
        # Persist the complete adapter response for every domain, even when the
        # adapter does not write its own trajectory artifact.
        write(directory / 'result.json', response)
        graded = suite.grade(case, prediction, directory)
        if not isinstance(graded, dict) or type(graded.get('passed')) is not bool:
            raise ValueError('Domain grader must return a boolean passed result')
        graded.setdefault('error', None)
    except TRIAL_ERRORS as exc:
        graded = dict(passed=False, error=str(exc))
    item = dict(task=case, prediction=prediction, **graded)
    write(directory / 'evaluation.json', item)
    if item.get('error'):
        log(f'EVAL {case["id"]} ERROR; details={directory}')
    return item


def evaluate_trials(workspace, jobs, model, timeout, suite, *, workers=50, executor=None, task_runner=None):
    """Jobs are (case, directory), including different repeats of the same case.

    Cached environments enter the trial pool immediately. Missing environments
    use a separate single-worker preparation pool, shared across repetitions.
    Completion order affects progress only; results preserve input ordering.
    """
    if type(workers) is not int or workers < 1 or not jobs:
        raise ValueError('Evaluation requires trials and positive workers')
    if executor is None:
        with ThreadPoolExecutor(max_workers=workers) as local:
            return evaluate_trials(workspace, jobs, model, timeout, suite, workers=workers,
                                   executor=local, task_runner=task_runner)
    log(f'EVAL: queued={len(jobs)} independent trials, workers={workers}')
    futures = {}
    results = [None] * len(jobs)
    completed, correct = 0, 0
    def submit(indices, error=None):
        for index in indices:
            case, directory = jobs[index]
            future = executor.submit(evaluate_case, workspace, directory, model, timeout, suite,
                                     case, task_runner, preparation_error=error)
            futures[future] = ('trial', index)

    with ThreadPoolExecutor(max_workers=1) as preparation:
        try:
            groups = {}
            for index, (case, _) in enumerate(jobs):
                key = case['id'] if task_runner is None and suite.prepare_environment is not None else index
                groups.setdefault(key, []).append(index)
            for indices in groups.values():
                case, directory = jobs[indices[0]]
                try:
                    if (task_runner is None and suite.prepare_environment is not None
                            and (suite.environment_ready is None or not suite.environment_ready(case))):
                        future = preparation.submit(suite.prepare_environment, case, directory, model)
                        futures[future] = ('prepare', indices)
                    else:
                        submit(indices)
                except TRIAL_ERRORS as exc:
                    submit(indices, exc)
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    kind, index = futures.pop(future)
                    if kind == 'prepare':
                        try:
                            future.result()
                        except TRIAL_ERRORS as exc:
                            submit(index, exc)
                        else:
                            submit(index)
                        continue
                    item = future.result()
                    results[index] = item
                    completed += 1
                    correct += item['passed']
                    if completed == 1 or completed % 10 == 0 or completed == len(jobs):
                        log(f'EVAL: {completed}/{len(jobs)}, correct={correct}')
        except BaseException:
            # Running adapters retain ownership of their containers and receipts.
            # Cancel both queues before executor shutdown waits for active work.
            for future in futures:
                future.cancel()
            raise
    return results


def save_report(suite, items, directory):
    payload = dict(domain=suite.name, split=suite.split, score=sum(x['passed'] for x in items)/len(items),
                   total_requested=len(items), evaluated=len(items), total_correct=sum(x['passed'] for x in items),
                   cases=items, trajectory_path=str(directory.resolve()))
    write(directory / 'summary.json', payload)
    write(directory / 'index.json', report_index(payload))
    return payload


def evaluate(workspace, artifacts, model, timeout, suite, *, workers=50, executor=None, task_runner=None):
    jobs = [(case, artifacts / case['id']) for case in suite.cases]
    results = evaluate_trials(workspace, jobs, model, timeout, suite, workers=workers,
                              executor=executor, task_runner=task_runner)
    return save_report(suite, results, artifacts)

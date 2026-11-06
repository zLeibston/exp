"""One shared trial queue across repetitions; adapters own independent environments."""
from concurrent.futures import ThreadPoolExecutor
from pipeline.evaluation import evaluate, evaluate_trials


class ExecutionPool:
    def __init__(self, model, workers=1):
        self.workers = workers

    def __enter__(self):
        self.executor = ThreadPoolExecutor(max_workers=self.workers)
        return self

    def evaluate(self, workspace, artifacts, model, timeout, suite):
        return evaluate(workspace, artifacts, model, timeout, suite,
                        workers=self.workers, executor=self.executor)

    def evaluate_trials(self, workspace, jobs, model, timeout, suite):
        return evaluate_trials(workspace, jobs, model, timeout, suite,
                               workers=self.workers, executor=self.executor)

    def __exit__(self, *exc):
        self.executor.shutdown(wait=True, cancel_futures=True)

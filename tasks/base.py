"""Domain-neutral evaluation contract; adapters own environments and trusted grading."""
from dataclasses import dataclass
from typing import Callable


@dataclass
class TaskSuite:
    cases: list[dict]
    name: str = "external"
    split: str = "train"
    prepare_case: Callable | None = None
    grade_case: Callable | None = None
    task_runner: Callable | None = None
    setup_environment: Callable | None = None
    network_policy: str = "adapter-managed"

    def prepare(self, case, directory):
        return self.prepare_case(case, directory) if self.prepare_case else dict(case["inputs"])

    def setup(self):
        return self.setup_environment(self.cases) if self.setup_environment else {}

    def run(self, workspace, directory, model, timeout, inputs):
        if self.task_runner is None:
            raise ValueError("Task suite requires an execution adapter")
        return self.task_runner(workspace, directory, model, timeout, inputs)

    def grade(self, case, prediction, directory):
        if self.grade_case:
            return self.grade_case(case, prediction, directory)
        raise ValueError("Task suite requires a trusted grader")


def validate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError("Tasks must be a nonempty list")
    ids = set()
    for case in cases:
        identifier = case.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in {".", ".."} or not all(c.isalnum() or c in "-_" for c in identifier):
            raise ValueError("Task id must be a safe directory name")
        if identifier in ids:
            raise ValueError("Duplicate task id")
        ids.add(identifier)
        if not isinstance(case.get("inputs"), dict):
            raise ValueError("Task inputs must be a mapping")
    return cases



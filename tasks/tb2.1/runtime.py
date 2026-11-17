"""Use the terminal executor with the benchmark's declared prebuilt images."""
from tasks.terminal import runtime as terminal


def prepare_environment(case, directory, model):
    return terminal.prepare_environment(case, directory, dict(model, prebuilt_task_images=True))


def invoke_task(workspace, directory, model, timeout, inputs):
    return terminal.invoke_task(workspace, directory, dict(model, prebuilt_task_images=True), timeout, inputs)

"""Snapshot an edit, record its diff and validate entry contracts."""
import difflib
from pathlib import Path
from pipeline.docker_eval import execute
from pipeline.storage import log, label, snapshot, inventory, write


def invoke(workspace, artifacts, model, timeout, **request):
    from pipeline.docker_eval import invoke_meta
    return invoke_meta(workspace, artifacts, model, timeout, **request)


def edit_prefix(stage):
    """Each evolution stage owns exactly one independent implementation."""
    if stage in {"forgetting_repair", "stability_repair"}:
        return "harness/task/"
    raise ValueError(f"Unknown edit stage: {stage}")


def propose(parent, candidate, artifacts, model, timeout, stage, materials, history, remaining):
    prefix = edit_prefix(stage)
    log(f"EDIT {label(candidate)}: {stage} started; chat={artifacts / 'agent/chat.log'}")
    snapshot(parent, candidate)
    before = inventory(candidate)
    artifacts.mkdir(parents=True, exist_ok=True)
    instruction = None
    invocation_error = None
    try:
        invoke(candidate, artifacts / "agent", model, timeout, kind="meta", stage=stage,
               runtime_workspace=str(parent),
               context_paths=[str(path) for path in (materials or ())], instruction=instruction,
               history_path=str(history) if history is not None else None, iterations_left=remaining)
    except Exception as exc:
        invocation_error = exc
    after = inventory(candidate)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    diffs = []
    for name in changed:
        try:
            old = (parent / name).read_text().splitlines(keepends=True) if (parent / name).is_file() else []
            new = (candidate / name).read_text().splitlines(keepends=True) if (candidate / name).is_file() else []
            if name in before and name in after and before[name]["mode"] != after[name]["mode"]:
                diffs.append(f"Mode changed: {name} {before[name]['mode']:03o} -> {after[name]['mode']:03o}\n")
            diffs.extend(difflib.unified_diff(old, new, fromfile="parent/" + name, tofile="candidate/" + name))
        except UnicodeDecodeError:
            diffs.append(f"Binary file changed: {name}\n")
    (artifacts / "changes.diff").write_text("".join(diffs))
    if invocation_error is not None:
        raise invocation_error
    if any(not p.startswith(prefix) for p in changed):
        raise ValueError(f"{stage}: expected changes only inside {prefix}, got {changed}")
    for name in ("task/task_agent.py", "meta/improve_agent.py"):
        if not (candidate / "harness" / name).is_file():
            raise ValueError(f"Missing harness entry point: harness/{name}")
    check(candidate, timeout, artifacts / "imports.log")
    write(artifacts / "changes.json", dict(stage=stage, files=changed))
    log(f"EDIT {label(candidate)}: {stage} completed, changed_files={len(changed)}")
    return candidate


PROBE = '''
import inspect
from pathlib import Path
for path in sorted(Path("/engine/harness").rglob("*.py")):
    compile(path.read_bytes(), str(path), "exec")
print("Harness Python compilation OK")
from harness.task.task_agent import TaskAgent
from harness.meta.improve_agent import ImproveAgent
for cls, args, kwargs in (
    (TaskAgent, ({},), {}),
    (ImproveAgent, (),
     dict(repo_path="/candidate", eval_path="/history", iterations_left=1,
          stage="stability_repair", context_paths=(), instruction=None)),
):
    assert inspect.isclass(cls) and not inspect.isabstract(cls), "Entry must be a concrete class"
    inspect.signature(cls).bind(model="probe", chat_history_file="/tmp/probe.log")
    method = getattr(cls, "forward", None)
    assert callable(method), "Entry forward must be callable"
    assert not any(test(method) for test in (inspect.iscoroutinefunction,
                   inspect.isgeneratorfunction, inspect.isasyncgenfunction)), "Worker requires a direct synchronous result"
    # Bind exactly as the worker does, without constructing the agent or making API calls.
    instance = object.__new__(cls)
    inspect.signature(getattr(instance, "forward")).bind(*args, **kwargs)
print("TaskAgent and ImproveAgent entry contracts OK")

# Import every runtime module with the other harness physically absent. A fresh,
# isolated interpreter prevents sys.modules or the original mount hiding coupling.
import shutil
import subprocess
import sys
import tempfile
for role in ("task", "meta"):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        package = root / "harness"
        package.mkdir()
        (package / "__init__.py").write_text("")
        shutil.copytree(Path("/engine/harness") / role, package / role,
                        ignore=shutil.ignore_patterns("__pycache__"))
        script = (
            "import importlib, pathlib, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "root = pathlib.Path(sys.argv[1]); "
            "modules = [p.relative_to(root).with_suffix('').parts "
            "for p in (root / 'harness' / sys.argv[2]).rglob('*.py')]; "
            "[importlib.import_module('.'.join(parts[:-1] if parts[-1] == '__init__' else parts)) "
            "for parts in modules]"
        )
        subprocess.run([sys.executable, "-I", "-c", script, temporary, role],
                       cwd=temporary, check=True)
print("Task and Meta isolated imports OK")
'''


def check(workspace, timeout, log_path=None):
    result = execute([(Path(workspace) / "harness", "/engine/harness", False)],
                     ["python", "-c", PROBE], cwd="/engine", timeout=timeout)
    if log_path is not None:
        Path(log_path).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise ValueError("Harness compilation or entry contract failed: " + result.stderr[-2000:])

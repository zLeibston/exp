"""Self-contained, task-organized Meta evidence; only this view enters Docker."""
import difflib
import json
from pathlib import Path
import shutil

from pipeline.docker_eval import validate_artifacts
from pipeline.scheduling import STAGES, repair_stage
from pipeline.storage import inventory, harness_digest, write

TRACE_FILES = ('chat.log', 'stdout.log', 'stderr.log', 'result.json')


def harness_diff(before, after):
    """Task-harness source and permission diff, never an evaluation archive diff."""
    old, new = Path(before) / 'harness/task', Path(after) / 'harness/task'
    before_files, after_files = inventory(old), inventory(new)
    chunks = []
    for name in sorted(before_files.keys() | after_files.keys()):
        if before_files.get(name) == after_files.get(name):
            continue
        if name in before_files and name in after_files and before_files[name]['mode'] != after_files[name]['mode']:
            chunks.append(f'Mode changed: {name} {before_files[name]["mode"]:03o} -> {after_files[name]["mode"]:03o}\n')
        try:
            a = (old / name).read_text().splitlines(keepends=True) if name in before_files else []
            b = (new / name).read_text().splitlines(keepends=True) if name in after_files else []
            chunks.extend(difflib.unified_diff(a, b, fromfile='previous/'+name, tofile='current/'+name))
        except UnicodeDecodeError:
            chunks.append(f'Binary file changed: {name}\n')
    return ''.join(chunks)



def transcript(source, target, *, task_id, observation, repetition, passed):
    """Render all normalized turns without truncation; retain raw result.json too."""
    raw = source / 'result.json'
    result = json.loads(raw.read_text()) if raw.is_file() else {}
    chunks = [f'# {task_id} / {observation} / repeat {repetition:02d}\n',
              f'Outcome: {"success" if passed else "failure"}\n',
              '## Prediction\n', '```json\n' + json.dumps(result.get('prediction'), ensure_ascii=False, indent=2) + '\n```\n']
    for number, message in enumerate(result.get('history', []), 1):
        chunks.append(f'## Turn {number:02d}: {message.get("role", "unknown")}\n')
        if message.get('name'):
            chunks.append(f'Tool: `{message["name"]}`; call: `{message.get("call_id", "")}`\n')
        text = message.get('text', '')
        chunks.append(text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, indent=2))
        for call in message.get('tool_calls', []):
            arguments = call.get('arguments', {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    pass
            chunks.append(f'\n### Tool call: {call.get("name", "unknown")} ({call.get("id", "")})\n')
            if call.get('name') == 'bash' and isinstance(arguments, dict) and isinstance(arguments.get('command'), str):
                chunks.append('```bash\n' + arguments['command'] + '\n```\n')
            else:
                chunks.append('```json\n' + json.dumps(arguments, ensure_ascii=False, indent=2) + '\n```\n')
    if not result.get('history') and (source / 'chat.log').is_file():
        chunks.extend(['## Chat log\n', (source / 'chat.log').read_text()])
    target.write_text('\n'.join(chunks) + '\n')


def build_view(destination, stage, cases, transitions, evaluated_workspace, candidate_workspace=None):
    """Only selected tasks, their observed harnesses and complete valid trials.

    Current harness always means the version actually evaluated this round.
    The writable candidate may additionally contain the preceding stability edit.
    """
    if stage not in STAGES:
        raise ValueError('Unknown repair stage')
    chosen = [entry for entry in transitions if entry['stage'] == stage]
    if not chosen or any(repair_stage(entry['before'], entry['after']) != stage for entry in chosen):
        raise ValueError('Repair view contains no matching transitions')
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    by_id = {case['id']: case for case in cases}
    candidate_workspace = candidate_workspace or evaluated_workspace
    candidate_differs = inventory(Path(candidate_workspace)/'harness/task') != inventory(Path(evaluated_workspace)/'harness/task')
    versions = {}

    def verify_observation(observation, current=False):
        workspace = Path(observation['workspace'])
        if str(workspace) not in versions:
            versions[str(workspace)] = harness_digest(workspace)
        identity = versions[str(workspace)]
        if identity != observation['harness_hash']:
            raise ValueError('Evidence harness changed since its evaluation')
        if current and workspace.resolve() != Path(evaluated_workspace).resolve():
            raise ValueError('Current trajectories do not belong to this round\'s evaluated harness')

    current_harness = destination / 'current_harness'
    validate_artifacts(Path(evaluated_workspace) / 'harness/task')
    shutil.copytree(Path(evaluated_workspace) / 'harness/task', current_harness,
                    ignore=shutil.ignore_patterns('__pycache__'))

    def copy_observation(identifier, name, observation, harness):
        target = destination / 'tasks' / identifier / name
        target.mkdir(parents=True)
        trials = []
        for trial in sorted(observation['attempts'], key=lambda item: item['repeat']):
            if trial['error'] is not None:
                continue
            source = Path(trial['trajectory'])
            if not (source/'result.json').is_file() and not (source/'chat.log').is_file():
                raise ValueError(f'Missing trajectory for {identifier}, repeat {trial["repeat"]}')
            validate_artifacts(source)
            outcome = 'success' if trial['passed'] else 'failure'
            trace = target / outcome / f'repeat_{trial["repeat"]:02d}'
            trace.mkdir(parents=True)
            for filename in TRACE_FILES:
                if (source / filename).is_file():
                    shutil.copy2(source / filename, trace / filename)
            transcript(source, trace / 'trajectory.md', task_id=identifier, observation=name,
                       repetition=trial['repeat'], passed=trial['passed'])
            write(trace / 'outcome.json', dict(repeat=trial['repeat'], passed=trial['passed'],
                                              round=observation['round'], harness_hash=observation['harness_hash']))
            trials.append(dict(repeat=trial['repeat'], passed=trial['passed'], trajectory=str(trace)))
        if (len(trials) != observation['evaluations'] or len({t['repeat'] for t in trials}) != len(trials)
                or sum(t['passed'] for t in trials) != observation['correct']):
            raise ValueError('Repair evidence must contain every valid repeated trial exactly once')
        summary = dict(round=observation['round'], correct=observation['correct'], evaluations=observation['evaluations'],
                       harness=str(harness), trials=trials)
        write(target / 'summary.json', summary)
        return summary

    index = dict(stage=stage, current_harness=str(current_harness),
                 candidate_differs_from_evaluated=candidate_differs, tasks=[])
    table = ['# Task comparison\n', '| Task | Previous success | Current success | Comparison |',
             '| --- | --- | --- | --- |']
    for entry in chosen:
        identifier = entry['task_id']
        verify_observation(entry['current'], current=True)
        task_dir = destination / 'tasks' / identifier
        task_dir.mkdir(parents=True)
        write(task_dir / 'task.json', dict(task_id=identifier, inputs=by_id[identifier]['inputs']))
        item = dict(task_id=identifier, task_path=str(task_dir / 'task.json'),
                    comparison=str(task_dir / 'comparison.md'),
                    current=copy_observation(identifier, 'current', entry['current'], current_harness))
        if stage == 'forgetting_repair':
            verify_observation(entry['previous'])
            old_source = Path(entry['previous']['workspace']) / 'harness/task'
            validate_artifacts(old_source)
            previous_harness = task_dir / 'previous_harness'
            shutil.copytree(old_source, previous_harness, ignore=shutil.ignore_patterns('__pycache__'))
            item['previous'] = copy_observation(identifier, 'previous', entry['previous'], previous_harness)
            difference = task_dir / 'harness.diff'
            delta = harness_diff(entry['previous']['workspace'], candidate_workspace)
            difference.write_text(delta)
            item.update(harness_diff=str(difference), harness_changed=bool(delta))
            if candidate_differs:
                evaluated_diff = task_dir/'evaluated.diff'
                evaluated_diff.write_text(harness_diff(entry['previous']['workspace'], evaluated_workspace))
                item['evaluated_diff'] = str(evaluated_diff)
        write(task_dir / 'comparison.json', item)
        lines = [f'# {identifier}\n',
                 '| Observation | Success | Harness |', '| --- | --- | --- |']
        for name in ('previous', 'current'):
            if name not in item:
                continue
            observation = item[name]
            lines.append(f'| {name} | {observation["correct"]}/{observation["evaluations"]} | `{observation["harness"]}` |')
        if 'harness_diff' in item:
            lines.extend(['', f'Previous → starting candidate: `harness.diff`; changed: {item["harness_changed"]}'])
            if 'evaluated_diff' in item:
                lines.append('Previous → evaluated harness: `evaluated.diff`')
        lines.extend(['', '| Repeat | Previous | Current |', '| --- | --- | --- |'])
        def trial_link(observation, repeat):
            trial = next((t for t in observation.get('trials', []) if t['repeat'] == repeat), None)
            if trial is None:
                return '—'
            path = Path(trial['trajectory']).relative_to(task_dir) / 'trajectory.md'
            return f'[{"success" if trial["passed"] else "failure"}]({path})'
        repetitions = sorted({t['repeat'] for name in ('previous', 'current')
                              for t in item.get(name, {}).get('trials', [])})
        for repetition in repetitions:
            lines.append(f'| {repetition:02d} | {trial_link(item.get("previous", {}), repetition)} | {trial_link(item["current"], repetition)} |')
        (task_dir / 'comparison.md').write_text('\n'.join(lines) + '\n')
        previous = item.get('previous')
        old_score = f'{previous["correct"]}/{previous["evaluations"]}' if previous else '—'
        table.append(f'| {identifier} | {old_score} | {entry["correct"]}/{entry["evaluations"]} | [compare](tasks/{identifier}/comparison.md) |')
        index['tasks'].append(item)
    write(destination / 'index.json', index)
    (destination / 'comparison.md').write_text('\n'.join(table) + '\n')
    return destination


def export_view(source, target):
    """Allowlist the guest view; never publish extra files from host bookkeeping."""
    index = json.loads((source/'index.json').read_text())
    forgetting = index['stage'] == 'forgetting_repair'
    target.mkdir()
    def public(value):
        if isinstance(value, dict):
            return {key: public(item) for key, item in value.items()
                    if key not in {'stage', 'round', 'harness_hash'}}
        if isinstance(value, list):
            return [public(item) for item in value]
        if isinstance(value, str):
            return value.replace(str(source), '/materials')
        return value
    def copy_file(relative):
        src, dst = source/relative, target/relative
        if not src.is_file():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text().replace(str(source), '/materials'))
    shutil.copytree(source/'current_harness', target/'current_harness',
                    ignore=shutil.ignore_patterns('__pycache__'))
    copy_file(Path('comparison.md'))
    entries = []
    for item in index['tasks']:
        identifier = item['task_id']
        if Path(identifier).name != identifier or identifier in {'.','..'}:
            raise ValueError('Unsafe task identifier in material view')
        root = Path('tasks')/identifier
        for name in ('task.json','comparison.md'):
            copy_file(root/name)
        keys = {'task_id','task_path','comparison','current'}
        if forgetting:
            keys |= {'previous','harness_diff','harness_changed','evaluated_diff'}
            shutil.copytree(source/root/'previous_harness', target/root/'previous_harness',
                            ignore=shutil.ignore_patterns('__pycache__'))
            copy_file(root/'harness.diff')
            if 'evaluated_diff' in item:
                copy_file(root/'evaluated.diff')
        entry = public({key:value for key,value in item.items() if key in keys})
        for observation in ('current','previous') if forgetting else ('current',):
            data = item[observation]
            summary = {key:data[key] for key in ('correct','evaluations','harness')}
            summary['trials'] = [{key:trial[key] for key in ('repeat','passed','trajectory')}
                                 for trial in data['trials']]
            entry[observation] = public(summary)
            write(target/root/observation/'summary.json', entry[observation])
            for trial in item[observation]['trials']:
                outcome = 'success' if trial['passed'] else 'failure'
                relative = root/observation/outcome/f'repeat_{trial["repeat"]:02d}'
                if Path(trial['trajectory']) != source/relative:
                    raise ValueError('Material trajectory escapes its task/observation')
                for name in (*TRACE_FILES,'trajectory.md'):
                    copy_file(relative/name)
                write(target/relative/'outcome.json', dict(repeat=trial['repeat'], passed=trial['passed']))
        write(target/root/'comparison.json', entry)
        entries.append(entry)
    write(target/'index.json', dict(current_harness='/materials/current_harness',
          candidate_differs_from_evaluated=index['candidate_differs_from_evaluated'], tasks=entries))

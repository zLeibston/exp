"""Meta objectives, material navigation and response instructions."""

STABILITY_REPAIR_INSTRUCTION = (
    'Improve stability in the task harness. Compare successful and failed runs of each task '
    'under the same harness, identify why their behavior differs, and improve the candidate harness.'
)

FORGETTING_REPAIR_INSTRUCTION = (
    'Repair forgetting in the task harness. Compare each task\'s current and previous runs, '
    'using the historical harness and diff to diagnose lost behavior and repair the candidate harness.'
)

STAGE_INSTRUCTIONS = {
    'stability_repair': STABILITY_REPAIR_INSTRUCTION,
    'forgetting_repair': FORGETTING_REPAIR_INSTRUCTION,
}

MATERIALS_GUIDE = (
    'Material directory guide:\n'
    '- comparison.md / index.json: overview of the selected tasks.\n'
    '- current_harness/: the read-only harness that produced this round\'s runs.\n'
    '- tasks/<id>/task.json: task input; comparison.md: results and links for that task.\n'
    '- tasks/<id>/current/{success,failure}/repeat_XX/: individual runs; '
    'trajectory.md is readable, result.json contains the full history.\n'
)

STAGE_MATERIALS_GUIDE = {
    'stability_repair': '',
    'forgetting_repair': (
        '- tasks/<id>/previous/: runs before regression, grouped in the same way.\n'
        '- tasks/<id>/previous_harness/: that task\'s historical harness. '
        'harness.diff compares it with the starting candidate; evaluated.diff, when present, '
        'compares it with current_harness/ for interpreting the recorded runs.\n'
        '\nChanges in the candidate beyond current_harness/ have not been re-evaluated.\n'
    ),
}

EDIT_REQUIREMENTS = (
    'Edit only the candidate Task Harness and preserve its entry interface. '
    'Make reusable changes, check the code, and briefly explain the cause and your edits.'
)

FORMAT_RETRY_INSTRUCTION = 'Use a native tool call for actions, or return one JSON object containing response.'


def improve_instruction(repo_path, eval_path, iterations_left, context_paths=(), *, stage):
    if stage not in STAGE_INSTRUCTIONS:
        raise ValueError(f'Unsupported improvement stage: {stage}')
    paths = [f'Candidate harness to modify: `{repo_path}`.']
    if eval_path is not None:
        paths.append(f'Read-only materials root: `{eval_path}`.')
    paths.extend(f'Current material (read-only): `{path}`.' for path in context_paths)
    sections = [STAGE_INSTRUCTIONS[stage], '\n'.join(paths),
                MATERIALS_GUIDE + STAGE_MATERIALS_GUIDE[stage], EDIT_REQUIREMENTS]
    return '\n\n'.join(section.rstrip() for section in sections)


def response_protocol_prompt(tool_infos=None):
    text = 'Finish with exactly one JSON object: {"response": "brief summary"}.'
    return 'Use native tools for actions. ' + text if tool_infos else text


def turn_instruction(remaining):
    if remaining is None:
        return 'Continue using tools, or finish with a response JSON.'
    text = f'Tool calls remaining: {remaining}.'
    if remaining <= 10:
        text += ' Complete your edits and checks.'
    return text

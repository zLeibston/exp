"""Stage-selected skill entrypoints; references are read by the agent on demand."""
from pathlib import Path


STAGE_SKILLS = {
    'stability_repair': 'stability-optimization',
    'forgetting_repair': 'forgetting-repair',
}


def load_skill(stage):
    try:
        name = STAGE_SKILLS[stage]
    except KeyError:
        raise ValueError(f'Unsupported Meta skill stage: {stage}') from None
    path = Path(__file__).resolve().parent / name / 'SKILL.md'
    text = path.read_text().strip()
    if not text:
        raise ValueError(f'Empty Meta skill: {name}')
    return path, text

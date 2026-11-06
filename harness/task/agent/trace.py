"""Bounded execution metadata and an incremental crash journal, without log bodies."""
from contextvars import ContextVar
import json
from pathlib import Path
import time

ACTIVE = ContextVar('task_trace', default=None)
EVENT_LIMIT = 1024 * 1024
JOURNAL_LIMIT = 32 * 1024 * 1024


def annotate(**fields):
    recorder = ACTIVE.get()
    if recorder is not None:
        recorder.tool_fields.update(fields)


class Trace:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.started = time.monotonic()
        self.iteration = 0
        self.messages = 0
        self.sequence = 0
        self.tool_fields = {}
        self.bytes = {'events.jsonl': 0, 'history.partial.jsonl': 0}
        self.limited = set()
        self.status = 'running'
        self.reason = None

    def append(self, name, value, limit):
        if name in self.limited:
            return
        data = (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        if self.bytes[name] + len(data) > limit:
            self.limited.add(name)
            self.summary()
            return
        with (self.directory/name).open('ab') as handle:
            handle.write(data)
        self.bytes[name] += len(data)

    def event(self, event, **fields):
        self.sequence += 1
        self.append('events.jsonl', dict(seq=self.sequence, elapsed_seconds=round(time.monotonic()-self.started, 4),
                    iteration=self.iteration, event=event, **fields), EVENT_LIMIT)

    def history(self, history):
        for index in range(self.messages, len(history)):
            self.append('history.partial.jsonl', dict(message_index=index, message=history[index]), JOURNAL_LIMIT)
        self.messages = len(history)
        self.summary()

    def summary(self):
        path = self.directory/'execution.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(schema=1, status=self.status, stop_reason=self.reason,
            model_iterations=self.iteration, history_messages=self.messages,
            elapsed_seconds=round(time.monotonic()-self.started, 4),
            limits_bytes=dict(events=EVENT_LIMIT, partial_history=JOURNAL_LIMIT),
            limited_files=sorted(self.limited)), indent=2))
        temporary.replace(path)

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        for name in self.bytes:
            (self.directory/name).write_bytes(b'')
        self.token = ACTIVE.set(self)
        self.summary()
        return self

    def __exit__(self, kind, value, traceback):
        self.status = 'failed' if kind else 'completed'
        self.reason = self.reason or ('exception' if kind else 'final_response')
        self.event('run_end', status=self.status, reason=self.reason,
                   error_type=kind.__name__ if kind else None)
        self.summary()
        ACTIVE.reset(self.token)

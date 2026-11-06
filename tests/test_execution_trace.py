"""Bounded task evidence without repeating tool payloads in event metadata."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from harness.task.agent.trace import Trace
from harness.task.agent.tools import bash, edit

class ExecutionTraceTests(unittest.TestCase):
    def test_tool_metadata_and_bounded_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with Trace(root) as trace:
                self.assertIn('exit_code: 7', bash.tool_function('printf trace-marker; exit 7'))
                self.assertEqual(trace.tool_fields['exit_code'], 7)
                trace.event('tool_end', **trace.tool_fields)
                trace.tool_fields = {}
                self.assertIn('<response clipped>', edit.maybe_truncate('x'*100, 20))
                self.assertEqual(trace.tool_fields['output_chars_before_clipping'], 100)
                trace.event('tool_end', **trace.tool_fields)
                with patch('harness.task.agent.trace.EVENT_LIMIT', 500):
                    for _ in range(100): trace.event('sample', count=1)
            self.assertLessEqual((root/'events.jsonl').stat().st_size, 500)
            self.assertNotIn('trace-marker', (root/'events.jsonl').read_text())
            self.assertIn('events.jsonl', json.loads((root/'execution.json').read_text())['limited_files'])

    def test_output_overflow_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp, Trace(tmp) as trace:
            with patch.object(bash, 'MAX_OUTPUT_CHARS', 100):
                result = bash.tool_function("python3 -c 'print(\"x\"*1000)'")
            self.assertTrue(result.startswith('Error'))
            self.assertTrue(trace.tool_fields['output_discarded'])
            self.assertGreater(trace.tool_fields['observed_chars_lower_bound'], 100)

    def test_partial_history_is_incremental_and_failure_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = [dict(role='user', text='first')]
            with self.assertRaises(RuntimeError):
                with Trace(root) as trace:
                    trace.history(history)
                    history.append(dict(role='assistant', text='second', native=dict(opaque='kept')))
                    trace.history(history)
                    trace.history(history)
                    raise RuntimeError('fixture')
            records = [json.loads(line) for line in (root/'history.partial.jsonl').read_text().splitlines()]
            self.assertEqual([r['message_index'] for r in records], [0,1])
            self.assertEqual(records[1]['message']['native'], {'opaque':'kept'})
            self.assertEqual(json.loads((root/'execution.json').read_text())['status'], 'failed')

    def test_partial_history_limit_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp, patch('harness.task.agent.trace.JOURNAL_LIMIT', 100):
            with Trace(tmp) as trace:
                trace.history([dict(role='user', text='x'*1000)])
            self.assertEqual((Path(tmp)/'history.partial.jsonl').stat().st_size, 0)
            self.assertIn('history.partial.jsonl', json.loads((Path(tmp)/'execution.json').read_text())['limited_files'])

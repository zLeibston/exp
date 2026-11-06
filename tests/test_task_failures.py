"""Worker failures retain evidence; service errors still request a retry."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pipeline import api_config, worker
from harness.task.agent.providers import APIError, BudgetExceeded


class TaskFailureTests(unittest.TestCase):
    def test_worker_distinguishes_agent_failure_from_api_failure(self):
        for error in (RuntimeError('tool budget exhausted'), ValueError('invalid response'),
                      BudgetExceeded('output token budget'), APIError('HTTP 429')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                message = dict(role='assistant', text='partial work')
                (root/'history.partial.jsonl').write_text(json.dumps(dict(message=message))+'\n')
                request = root/'request.json'
                request.write_text(json.dumps(dict(kind='task', workspace=str(root),
                    model=dict(config_path='unused'), inputs={}, log_path=str(root/'chat.log'),
                    result_path=str(root/'result.json'))))
                with patch.dict(sys.modules, api_config=api_config), \
                     patch.object(sys, 'argv', ['worker', str(request)]), \
                     patch.dict('os.environ'), \
                     patch.object(api_config, 'load_config', return_value={'task_agent': SimpleNamespace(model='fixture')}), \
                     patch.object(worker, 'install_api_diagnostics'), \
                     patch('harness.task.task_agent.TaskAgent') as agent:
                    agent.return_value.forward.side_effect = error
                    if type(error) is APIError:
                        with self.assertRaises(APIError): worker.main()
                        self.assertFalse((root/'result.json').exists())
                    else:
                        worker.main()
                        result = json.loads((root/'result.json').read_text())
                        self.assertEqual(result['history'], [message])
                        self.assertEqual(result['task_failure']['error_type'], type(error).__name__)

    def test_killed_journal_keeps_complete_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'history.partial.jsonl').write_text('{"message":{"text":"kept"}}\n{"message":')
            result = worker.failed_task_result(root, 'agent_timeout')
            self.assertEqual(result['history'], [dict(text='kept')])
            self.assertEqual(result['task_failure']['reason'], 'agent_timeout')

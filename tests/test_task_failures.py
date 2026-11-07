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
    def test_deadline_classification_requires_explicit_unresolved_api_failure(self):
        response = lambda status: dict(event='response', http_status=status)
        pending = dict(event='request_started')
        transport = dict(event='transport_error')
        for records, expected in (([],False), ([pending],False), ([response(429),pending],True),
                                  ([transport,pending],True), ([response(503)],True),
                                  ([response(429),response(200),pending],False),
                                  ([transport,response(200)],False),
                                  ([dict(event='response',http_status=400,context_limit=True)],False)):
            with self.subTest(records=records), tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'api_calls.jsonl'
                path.write_text(''.join(json.dumps(record)+'\n' for record in records)+'{"partial":')
                self.assertEqual(worker.unresolved_api_failure(path),expected)
        self.assertFalse(worker.unresolved_api_failure(Path(tmp)/'missing'))

    def test_explicit_context_limits_are_agent_failures(self):
        import httpx
        errors=[dict(code='context_length_exceeded'),dict(type='context_window_exceeded'),
                dict(type='invalid_request_error',message='prompt is too long: 201000 tokens'),
                dict(status='INVALID_ARGUMENT',message='The input token count exceeds the maximum number of tokens allowed')]
        cases=[(400,error,True) for error in errors]+[(status,errors[0],False) for status in (401,429,500)]
        for status,error,expected in cases:
            with self.subTest(status=status,error=error),tempfile.TemporaryDirectory() as tmp:
                original=httpx.Client.send
                try:
                    path=Path(tmp)/'api.jsonl'
                    state=worker.install_api_diagnostics(path,'task_agent','fixture')
                    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(status,json={'error':error}))) as client:
                        client.post('https://fixture.test',json={})
                    self.assertEqual(state['context_limit'],expected)
                    self.assertEqual(state['failed'],not expected)
                    self.assertEqual(worker.unresolved_api_failure(path),not expected)
                    record=json.loads(path.read_text().splitlines()[-1])
                    self.assertNotIn('message',record)
                finally:httpx.Client.send=original

    def test_http_observer_tracks_failure_recovery_and_fatal_balance(self):
        import httpx
        with tempfile.TemporaryDirectory() as tmp:
            original = httpx.Client.send
            try:
                state = worker.install_api_diagnostics(Path(tmp)/'api.jsonl', 'task_agent', 'fixture')
                responses = iter((429, 200, 402))
                def reply(request):
                    return httpx.Response(next(responses), json={'choices': []})
                with httpx.Client(transport=httpx.MockTransport(reply)) as client:
                    client.post('https://fixture.test', json={})
                    self.assertTrue(state['failed'])
                    client.post('https://fixture.test', json={})
                    self.assertFalse(state['failed'])
                    with self.assertRaises(SystemExit) as stopped:
                        client.post('https://fixture.test', json={})
                    self.assertEqual(stopped.exception.code, 78)
                    self.assertTrue(state['fatal'])
            finally:
                httpx.Client.send = original

    def test_worker_distinguishes_agent_failure_from_api_failure(self):
        import httpx
        errors = (RuntimeError('tool budget exhausted'), ValueError('invalid response'),
                      BudgetExceeded('output token budget'), APIError('HTTP 429'),
                      SystemExit(0), SystemExit(1), SystemExit(78))
        request=httpx.Request('POST','https://fixture.test')
        contexts=(APIError('context limit'), httpx.HTTPStatusError('context limit',request=request,
                  response=httpx.Response(400,request=request)))
        for error,context_limit in [*( (e,False) for e in errors), *( (e,True) for e in contexts)]:
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
                     patch.object(worker, 'install_api_diagnostics',
                                  return_value=dict(failed=type(error) is APIError and not context_limit,
                                                    fatal=False,context_limit=context_limit)), \
                     patch('harness.task.task_agent.TaskAgent') as agent:
                    agent.return_value.forward.side_effect = error
                    if type(error) is APIError and not context_limit:
                        with self.assertRaises(APIError): worker.main()
                        self.assertFalse((root/'result.json').exists())
                    else:
                        worker.main()
                        result = json.loads((root/'result.json').read_text())
                        self.assertEqual(result['history'], [message])
                        self.assertEqual(result['task_failure']['error_type'], type(error).__name__)
                        if context_limit:self.assertEqual(result['task_failure']['reason'],'context_limit')

    def test_killed_journal_keeps_complete_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'history.partial.jsonl').write_text('{"message":{"text":"kept"}}\n{"message":')
            result = worker.failed_task_result(root, 'agent_timeout')
            self.assertEqual(result['history'], [dict(text='kept')])
            self.assertEqual(result['task_failure']['reason'], 'agent_timeout')

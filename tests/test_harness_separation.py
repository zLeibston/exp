"""Regression checks for independent runtimes and stage ownership.

Run: python -m unittest discover -s tests -v (from exp/, with requirements installed).
All model responses come from a local HTTP fixture; no external API is called.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline import editing
from pipeline.storage import snapshot


class SeparationTests(unittest.TestCase):
    def test_worker_runs_with_only_its_own_harness(self):
        for stage in ('task', 'forgetting_repair', 'stability_repair'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                role = 'task' if stage == 'task' else 'meta'
                api_role = 'task_agent' if role == 'task' else 'meta_agent'
                runtime = root / 'runtime'
                (runtime / 'harness').mkdir(parents=True)
                (runtime / 'harness/__init__.py').write_text('')
                shutil.copytree(ROOT / 'harness' / role, runtime / 'harness' / role)
                candidate = root / 'candidate'
                target = candidate / 'harness/task'
                target.mkdir(parents=True)
                marker = target / 'fixture.txt'
                requests = []

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_POST(self):
                        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        requests.append(body)
                        if role == 'meta' and len(requests) == 1:
                            message = dict(role='assistant', content=None, tool_calls=[dict(
                                id='edit-1', type='function', function=dict(name='editor', arguments=json.dumps(
                                    dict(command='create', path=str(marker), file_text='edited'))))])
                        else:
                            message = dict(role='assistant', content='{"response": "ok"}')
                        reply = json.dumps(dict(choices=[dict(message=message, finish_reason='stop')])).encode()
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(reply)))
                        self.end_headers()
                        self.wfile.write(reply)

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    profile = dict(model='fixture', api_key='fixture', provider='openai',
                                   url=f'http://127.0.0.1:{server.server_port}', retries=0)
                    # The harness itself must accept a config containing only its active role.
                    single = root / 'single.json'
                    single.write_text(json.dumps({api_role: profile}))
                    code = (f'from harness.{role}.agent.config import load_config; '
                            f'assert load_config({str(single)!r}, roles=({api_role!r},))')
                    result = subprocess.run([sys.executable, '-c', code], cwd=runtime,
                                            capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    # The fixed controller retains its full run-config schema.
                    config = root / 'api.json'
                    config.write_text(json.dumps(dict(task_agent=profile, meta_agent=profile)))
                    instruction = None
                    request = dict(kind='task' if role == 'task' else 'meta', stage=stage,
                                   workspace=str(candidate), runtime_workspace=str(runtime),
                                   model=dict(config_path=str(config)), api_role=api_role,
                                   log_path=str(root / 'chat.log'), result_path=str(root / 'result.json'),
                                   inputs=dict(question='fixture'), context_paths=[], history_path=None,
                                   instruction=instruction, iterations_left=1)
                    path = root / 'request.json'
                    path.write_text(json.dumps(request))
                    env = {key: value for key, value in os.environ.items()
                           if 'proxy' not in key.lower() and key != 'PYTHONPATH'}
                    result = subprocess.run([sys.executable, str(ROOT / 'pipeline/worker.py'), str(path)],
                                            cwd=root, env=env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    output = json.loads((root / 'result.json').read_text())
                    self.assertTrue(output['history'])
                    if role == 'task':
                        self.assertEqual(output['prediction'], 'ok')
                        self.assertNotIn('tools', requests[0])
                    else:
                        self.assertEqual(marker.read_text(), 'edited')
                        self.assertIn(str(target), requests[0]['messages'][0]['content'])
                        if stage in {'forgetting_repair', 'stability_repair'}:
                            from harness.meta.prompt import STAGE_INSTRUCTIONS
                            prompt = requests[0]['messages'][0]['content']
                            self.assertIn(STAGE_INSTRUCTIONS[stage], prompt)
                            other = 'stability_repair' if stage == 'forgetting_repair' else 'forgetting_repair'
                            self.assertNotIn(STAGE_INSTRUCTIONS[other], prompt)
                        self.assertEqual(len(requests), 2)
                        self.assertTrue(any(m['role'] == 'tool' for m in requests[1]['messages']))
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()

    def test_stages_reject_edits_to_other_harness(self):
        for stage in ('forgetting_repair', 'stability_repair'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                parent = snapshot(ROOT, root / 'parent')
                wrong = 'meta'

                def invoke(candidate, *args, **kwargs):
                    (candidate / 'harness' / wrong / 'prompt.py').write_text('changed = True\n')

                with patch.object(editing, 'invoke', side_effect=invoke), patch.object(editing, 'check') as check:
                    with self.assertRaisesRegex(ValueError, 'only inside'):
                        editing.propose(parent, root / 'candidate', root / 'artifacts', {}, 10,
                                        stage, [], None, 1)
                    check.assert_not_called()

    def test_stages_accept_edits_to_owned_harness(self):
        for stage in ('forgetting_repair', 'stability_repair'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                parent = snapshot(ROOT, root / 'parent')

                def invoke(candidate, *args, **kwargs):
                    (candidate / editing.edit_prefix(stage) / 'fixture.py').write_text('changed = True\n')

                with patch.object(editing, 'invoke', side_effect=invoke), patch.object(editing, 'check') as check:
                    candidate = editing.propose(parent, root / 'candidate', root / 'artifacts', {}, 10,
                                               stage, [], None, 1)
                    check.assert_called_once()
                    self.assertTrue((candidate / editing.edit_prefix(stage) / 'fixture.py').is_file())

    def test_removed_stages_are_rejected(self):
        for stage in ('degrade', 'repair', 'task_improve', 'meta_reflect'):
            with self.subTest(stage=stage), self.assertRaisesRegex(ValueError, 'Unknown edit stage'):
                editing.edit_prefix(stage)

    def test_legacy_snapshot_fails_before_creating_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'old/harness').mkdir(parents=True)
            (root / 'old/harness/task_agent.py').write_text('')
            with self.assertRaisesRegex(ValueError, 'Legacy shared harness'):
                snapshot(root / 'old', root / 'candidate')
            self.assertFalse((root / 'candidate').exists())


if __name__ == '__main__':
    unittest.main()

"""Terminal pool contracts. Docker/API smoke is a separate offline fixture script."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tasks import load_domain
from tasks.terminal.loader import load, task_digest
from tasks.terminal import runtime
from pipeline.evaluation import evaluate


class TerminalTests(unittest.TestCase):
    def test_reviewed_pool_and_limits(self):
        combined = load_domain('terminal')
        tb, tbh = load_domain('terminal', dataset='tb'), load_domain('terminal', dataset='tbh')
        self.assertEqual((len(combined.cases), len(tb.cases), len(tbh.cases)), (202, 108, 94))
        self.assertEqual({c['id'] for c in combined.cases}, {c['id'] for c in tb.cases + tbh.cases})
        excluded = json.loads((ROOT / 'tasks/terminal/audit/selection.json').read_text())
        blocked = {f"{c['source']}__{c['task_id']}" for c in excluded['excluded'] + excluded['review']}
        self.assertFalse(blocked & {c['id'] for c in combined.cases})
        temporary = json.loads((ROOT / 'tasks/terminal/audit/runtime_exclusions.json').read_text())['tasks']
        self.assertEqual(len(temporary), 0)
        self.assertFalse({c['id'] for c in temporary} & {c['id'] for c in combined.cases})
        self.assertTrue(all((ROOT / 'tasks/terminal' / c['path']).is_dir() for c in temporary))
        migrations = json.loads((ROOT / 'tasks/terminal/audit/base_image_migrations.json').read_text())['tasks']
        self.assertEqual(len(migrations), 86)
        self.assertTrue({c['id'] for c in migrations} <= {c['id'] for c in combined.cases})
        import hashlib
        for entry in migrations:
            file = ROOT / 'tasks/terminal' / entry['path']
            current = file.read_bytes()
            self.assertEqual(hashlib.sha256(current).hexdigest(), entry['new_sha256'])
            restored = current.replace(entry['new_image'].encode(), entry['old_image'].encode())
            self.assertEqual(hashlib.sha256(restored).hexdigest(), entry['old_sha256'])
        for case in combined.cases:
            dockerfile = Path(case['task_path']) / 'environment/Dockerfile'
            self.assertFalse(any('skylensage-registry.cn-hangzhou.cr.aliyuncs.com' in line
                                 for line in dockerfile.read_text().splitlines()
                                 if line.lstrip().upper().startswith('FROM ')))
        self.assertEqual(len(load_domain('terminal', limit=3).cases), 3)
        with self.assertRaisesRegex(ValueError, 'training data only'):
            load_domain('terminal', split='test')

    def test_inputs_do_not_expose_verifier_or_reference(self):
        suite = load_domain('terminal', limit=2)
        with tempfile.TemporaryDirectory() as tmp:
            for case in suite.cases:
                inputs = suite.prepare(case, Path(tmp) / case['id'])
                public = {k: v for k, v in inputs.items() if not k.startswith('_')}
                self.assertNotIn('task_path', public)
                self.assertNotIn('data_hash', public)
                self.assertNotIn('tests', public)
                self.assertNotIn('solution', public)
                self.assertEqual(inputs['_data_hash'], task_digest(Path(case['task_path'])))

    def test_mutation_changes_image_identity_and_blocks_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'task.toml').write_text('')
            before = task_digest(root)
            first = runtime.image_tags(dict(data_hash=before))
            (root / 'task.toml').write_text('version="changed"')
            self.assertNotEqual(first, runtime.image_tags(dict(data_hash=task_digest(root))))
            with self.assertRaisesRegex(ValueError, 'changed after run setup'):
                runtime.ensure_image(root, before)

    def test_reward_is_required_and_binary(self):
        self.assertTrue(runtime.read_reward('1\n'))
        self.assertFalse(runtime.read_reward('0\n'))
        for value in ('', '0.5', 'true', 'nan', '1\n0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runtime.read_reward(value)

    def test_task_resources_cannot_exceed_run_limits(self):
        with patch.object(runtime, 'container_limits', return_value=dict(cpus=2, memory=4*1024**3, pids_limit=128)):
            limits = runtime.limits_for(dict(environment=dict(cpus=8, memory_mb=8192)), [])
            self.assertEqual(limits, dict(cpus=2, memory=4*1024**3, pids_limit=128))
            limits = runtime.limits_for(dict(environment=dict(cpus=1, memory_mb=2048)), [])
            self.assertEqual(limits['memory'], 2*1024**3)

    def test_existing_evaluator_uses_binary_grade_not_response_text(self):
        suite = load_domain('terminal', limit=2)
        calls = []
        def fake_runner(workspace, directory, model, timeout, inputs):
            calls.append(inputs['task_id'])
            (directory / 'verifier').mkdir()
            passed = len(calls) == 1
            (directory / 'verifier/result.json').write_text(json.dumps(dict(passed=passed, error=None)))
            return dict(prediction='I solved it', history=[])
        with tempfile.TemporaryDirectory() as tmp:
            result = evaluate(ROOT, Path(tmp) / 'eval', {}, 10, suite,
                              workers=1, task_runner=fake_runner)
        self.assertEqual(result['score'], 0.5)
        self.assertEqual(len(calls), 2)

    def test_generic_prompt_has_no_network_instructions(self):
        from harness.task.prompt import task_instruction, TASK_INSTRUCTION
        for inputs in ({}, dict(domain='terminal', allow_internet=False),
                       dict(domain='terminal', allow_internet=True), dict(domain='external')):
            self.assertEqual(task_instruction(inputs).split('\n\nTask input:')[0], TASK_INSTRUCTION)
        self.assertEqual(TASK_INSTRUCTION, 'You are an agent aimed at solving tasks. ')

    def test_task_network_policy(self):
        self.assertTrue(runtime.allows_internet({}))
        self.assertTrue(runtime.allows_internet(dict(environment=dict(allow_internet=True))))
        self.assertFalse(runtime.allows_internet(dict(environment=dict(allow_internet=False))))
        self.assertFalse(runtime.allows_internet(dict(environment=dict(allow_internet=True, network_mode='no-network'))))
        with self.assertRaises(ValueError):
            runtime.allows_internet(dict(environment=dict(allow_internet='false')))
        with self.assertRaises(ValueError):
            runtime.allows_internet(dict(environment=dict(network_mode='allowlist')))

    def test_only_terminal_seed_enables_existing_tools(self):
        from harness.task.task_agent import TaskAgent
        with tempfile.TemporaryDirectory() as tmp, patch('harness.task.task_agent.chat_with_agent') as chat:
            chat.return_value = [dict(role='assistant', text='{"response":"ok"}')]
            agent = TaskAgent(model='fixture', chat_history_file=str(Path(tmp) / 'chat.log'))
            agent.forward(dict(domain='terminal'))
            self.assertEqual(chat.call_args.kwargs['tools_available'], 'all')
            agent.forward(dict(domain='external'))
            self.assertIsNone(chat.call_args.kwargs['tools_available'])


if __name__ == '__main__':
    unittest.main()

"""TB2.1 discovery, private inputs, source identity and prebuilt image selection."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tasks import load_domain
from tasks.terminal import runtime
from tasks.terminal.loader import task_digest
from pipeline.storage import execution_manifest

ROOT = Path(__file__).resolve().parents[1]


class TB21Tests(unittest.TestCase):
    def test_registry_and_agent_boundary(self):
        suite = load_domain('tb2.1')
        self.assertEqual(len(suite.cases), 89)
        self.assertEqual(len({case['id'] for case in suite.cases}), 89)
        self.assertTrue(any('install-windows-3_11' in case['id'] for case in suite.cases))
        for case in suite.cases:
            self.assertEqual(case['inputs']['domain'], 'terminal')
            self.assertEqual(set(case['inputs']),
                             {'domain', 'source', 'task_id', 'problem_statement', 'test_description'})
        self.assertEqual(len(load_domain('tb2.1', split='test', limit=2).cases), 2)
        with self.assertRaises(ValueError):
            load_domain('../tb2.1')
        sources = execution_manifest(ROOT, 'tb2.1', suite.execution_dependencies)
        self.assertIn('tasks/tb2.1/loader.py', sources)
        self.assertIn('tasks/terminal/runtime.py', sources)
        self.assertFalse(any('/environment/' in name or '/solution/' in name for name in sources))

    def test_prebuilt_pull_and_resume_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'task.toml').write_text('[environment]\nbuild_timeout_sec = 60\n')
            digest = task_digest(root)
            commands = []

            def execute(args, **kwargs):
                commands.append(args)
                return subprocess.CompletedProcess(args, 1 if 'inspect' in args else 0)

            with patch.object(runtime, 'docker', return_value=['docker']), \
                    patch.object(runtime.subprocess, 'run', side_effect=execute):
                runtime.ensure_image(root, digest, source_image='example/task:version')
                self.assertIn(['docker', 'pull', 'example/task:version'], commands)
                self.assertEqual(sum('build' in args for args in commands), 1)  # Runner layer only.
                commands.clear()
                with self.assertRaisesRegex(RuntimeError, 'Previously used'):
                    runtime.ensure_image(root, digest, expected_id='sha256:original',
                                         source_image='example/task:version')
                self.assertFalse(any('pull' in args for args in commands))


if __name__ == '__main__':
    unittest.main()

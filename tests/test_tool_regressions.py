"""Both independent tool implementations preserve files and bound pipe cleanup."""
import asyncio
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

from harness.task.agent.tools import bash as task_bash
from harness.meta.agent.tools import bash as meta_bash, edit as meta_edit


class ToolRegressionTests(unittest.TestCase):
    def test_edits_and_undo_preserve_literal_tabs(self):
        for module in (meta_edit,):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)/'Makefile'
                original = 'all:\n\t@echo before\n'
                path.write_text(original)
                module.tool_function('str_replace', str(path), old_str='before', new_str='after')
                self.assertEqual(path.read_text(), original.replace('before', 'after'))
                module.tool_function('undo_edit', str(path))
                self.assertEqual(path.read_text(), original)
                module.tool_function('insert', str(path), insert_line=2, new_str='\t@echo extra')
                self.assertIn('\t@echo before\n\t@echo extra', path.read_text())
                module.tool_function('undo_edit', str(path))
                self.assertEqual(path.read_text(), original)

    def test_detached_child_cannot_hold_tool_cleanup_open(self):
        for module in (task_bash, meta_bash):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                pid_file = Path(tmp)/'pid'
                command = f"setsid sh -c 'echo $$ > {pid_file}; sleep 10' & sleep 0.1"
                started = time.monotonic()
                try:
                    with patch.object(module, 'CLEANUP_TIMEOUT', 0.1):
                        result = asyncio.run(module.tool_function_call(command))
                    self.assertIn('exit_code: 0', result)
                    self.assertLess(time.monotonic()-started, 2)
                finally:
                    if pid_file.exists():
                        try: os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                        except ProcessLookupError: pass

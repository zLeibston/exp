"""Resume freezes fixed execution code while allowing configuration and Harness evolution."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pipeline import app
from pipeline.evolution import LAYOUT
from pipeline.storage import execution_manifest
from tasks import TaskSuite


class ResumeCodeTests(unittest.TestCase):
    def test_execution_manifest_covers_code_but_excludes_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            sources=['run.py','requirements.txt','containers/agent.Dockerfile',
                     'pipeline/worker.py','tasks/base.py','tasks/__init__.py',
                     'tasks/terminal/runtime.py','tasks/terminal/helpers/extra.py']
            for name in sources:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('original')
            original=execution_manifest(root,'terminal')
            self.assertEqual(set(original),set(sources))
            for name in sources:
                path=root/name;path.write_text('changed')
                self.assertNotEqual(execution_manifest(root,'terminal'),original,name)
                path.write_text('original')
            for name in ('harness/task/task_agent.py','config.yaml','pipeline/README.md',
                         'tasks/terminal/data/task.py','tasks/terminal/env/cache.py',
                         'tasks/terminal/audit/proof.py'):
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('ignored')
            self.assertEqual(execution_manifest(root,'terminal'),original)
            extra=root/'pipeline/new.py';extra.write_text('new')
            self.assertNotEqual(execution_manifest(root,'terminal'),original)
            extra.unlink();(root/'pipeline/worker.py').unlink()
            self.assertNotEqual(execution_manifest(root,'terminal'),original)

    def test_resume_rejects_changed_or_missing_fingerprint_before_setup(self):
        for saved in (None, {'pipeline/worker.py':'old'}):
            with self.subTest(saved=saved),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);output=root/'run';output.mkdir()
                record=dict(layout=LAYOUT)
                if saved is not None: record['execution_code']=saved
                old=json.dumps(record);(output/'config.json').write_text(old)
                profile=dict(model='fixture',api_key='fixture',provider='openai')
                config=root/'fixture.yaml'
                config.write_text(json.dumps(dict(task_agent=profile,meta_agent=profile)))
                setup=Mock(side_effect=AssertionError('Environment setup must not start'))
                suite=TaskSuite(name='terminal',cases=[dict(id='fixture',inputs={})],setup_environment=setup)
                with patch.object(app,'load_domain',return_value=suite), \
                     patch.object(app,'execution_manifest',return_value={'pipeline/worker.py':'new'}), \
                     patch.object(app,'ensure_images') as images:
                    with self.assertRaisesRegex(ValueError,'Execution code'):
                        app.run(output,config=config,resume=True)
                setup.assert_not_called();images.assert_not_called()
                self.assertEqual((output/'config.json').read_text(),old)

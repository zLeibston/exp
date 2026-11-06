"""A second domain exercises the public adapter contract without Terminal imports."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import tasks
from tasks import load_domain, TaskSuite
from pipeline.evaluation import evaluate
from pipeline import evolution
from pipeline.storage import snapshot

ADAPTER = '''
import json
from tasks import TaskSuite

def run(workspace, directory, model, timeout, inputs):
    response = dict(prediction=str(sum(inputs['numbers'])), history=[])
    (directory/'chat.log').write_text('arithmetic fixture trajectory')
    return response

def grade(case, prediction, directory):
    return dict(passed=prediction == str(case['expected']), error=None)

def load(split='train', dataset=None):
    if dataset not in {None,'tiny'}: raise ValueError('Unknown arithmetic dataset')
    cases=[dict(id='sum', inputs=dict(numbers=[2,3]), expected=5)]
    return TaskSuite(cases=cases, name='arithmetic', split=split, task_runner=run,
                     grade_case=grade, network_policy='fixture-offline',
                     setup_environment=lambda cases: {'fixture': {'count': len(cases)}})
'''


class DomainInterfaceTests(unittest.TestCase):
    def test_new_domain_uses_common_evaluation_and_abc_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'arithmetic').mkdir();(root/'arithmetic/__init__.py').write_text(ADAPTER)
            with patch.object(tasks,'__path__',[*tasks.__path__,str(root)]):
                importlib.invalidate_caches()
                try:
                    suite=load_domain('arithmetic',dataset='tiny')
                    self.assertEqual(suite.network_policy,'fixture-offline')
                    self.assertEqual(suite.setup(),dict(fixture=dict(count=1)))
                    result=evaluate(ROOT,root/'evaluation',{},10,suite,workers=2)
                    self.assertEqual(result['score'],1)
                    self.assertEqual(result['domain'],'arithmetic')
                    artifacts=list((root/'evaluation').rglob('result.json'))
                    self.assertTrue(any(json.loads(path.read_text()).get('prediction')=='5' for path in artifacts))
                    initial=snapshot(ROOT,root/'run/initial')
                    with patch.object(evolution,'check'),patch.object(evolution,'propose') as edit:
                        state=evolution.evolve(root/'run',initial,suite,{},10,2,None,
                            dict(cold_start_repeats=4,repeats_per_task=4,quotas=dict(A=1,B=1,C=1)),2)
                    edit.assert_not_called()
                    self.assertEqual(state['tasks']['sum']['evaluation_count'],8)
                    self.assertEqual(state['tasks']['sum']['label'],'A')
                finally:
                    sys.modules.pop('tasks.arithmetic',None)

    def test_bad_domain_names_and_missing_factory_are_rejected(self):
        for name in ('../terminal','terminal.loader','base','missing_domain_fixture'):
            with self.subTest(name=name),self.assertRaises(ValueError):load_domain(name)

    def test_invalid_grader_result_is_an_error_not_a_success(self):
        suite=TaskSuite(cases=[dict(id='bad',inputs={})],name='fixture',
                        task_runner=lambda *args:dict(prediction='answer',history=[]),
                        grade_case=lambda *args:dict(passed=1))
        with tempfile.TemporaryDirectory() as tmp:
            result=evaluate(ROOT,Path(tmp)/'eval',{},10,suite,workers=1)
        self.assertFalse(result['cases'][0]['passed'])
        self.assertIn('boolean passed',result['cases'][0]['error'])


if __name__=='__main__':unittest.main()

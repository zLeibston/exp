"""Behavioral regressions for repeated RSI, resume, and stage evidence isolation."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tasks import TaskSuite
from pipeline import evolution
from pipeline.scheduling import (classify, repair_stage, select_tasks, load_settings,
                                 repeated_evaluation, DEFAULTS)
from pipeline.storage import snapshot, write


class FixturePool:
    calls = []
    failure = None

    def __init__(self, *args): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass

    def evaluate_trials(self, workspace, jobs, model, timeout, suite):
        results = []
        for case, trace in jobs:
            index = int(next(p.name[6:] for p in trace.parents if p.name.startswith('round_')))
            repeat = int(trace.parent.parent.name[7:])
            name = case['id']
            self.calls.append((index, repeat, name))
            if index == 1:
                passed = name.startswith('a') or (name.startswith('b') and repeat == 1)
            else:
                passed = name == 'a_keep' or (name == 'b_stable' and repeat <= 2) or name == 'b_improve'
            error = 'fixture environment failure' if self.failure == (index, repeat, name) else None
            trace.mkdir(parents=True)
            (trace / 'chat.log').write_text('TASK_TRACE:' + name)
            (trace / 'result.json').write_text(json.dumps(dict(prediction=name, history=[])))
            (trace / 'request.json').write_text('DO_NOT_EXPOSE_REQUEST')
            (trace / 'verifier.log').write_text('DO_NOT_EXPOSE_VERIFIER')
            item = dict(task=case, passed=passed, error=error)
            write(trace / 'evaluation.json', item)
            results.append(item)
        return results


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        FixturePool.calls, FixturePool.failure = [], None
        self.suite = TaskSuite(cases=[dict(id=name, inputs=dict(task_id=name)) for name in
                                    ('a_forget', 'a_keep', 'b_forget', 'b_stable', 'b_improve', 'c_keep')])
        self.settings = dict(cold_start_repeats=4, repeats_per_task=4, quotas=dict(A=5,B=10,C=5))

    def test_classification_and_all_transitions(self):
        self.assertEqual([classify(n, 4) for n in range(5)], ['C','B','B','B','A'])
        expected = {('A','B'):'forgetting_repair', ('A','C'):'forgetting_repair',
                    ('B','C'):'forgetting_repair', ('B','B'):'stability_repair'}
        for before in 'ABC':
            for after in 'ABC':
                self.assertEqual(repair_stage(before,after),expected.get((before,after)))

    def test_least_evaluated_per_class_and_shortage(self):
        stats = {str(n):dict(label=label,evaluation_count=count) for n,(label,count) in enumerate(
            [('A',8),('A',4),('A',4),('B',4),('B',12),('C',8)])}
        cases = [dict(id=str(n)) for n in range(6)]
        chosen, counts = select_tasks(cases,stats,dict(A=2,B=1,C=5))
        self.assertEqual([c['id'] for c in chosen],['1','2','3','5'])
        self.assertEqual(counts,dict(A=2,B=1,C=1))
        stats['1']['evaluation_count'] += 4
        chosen,_ = select_tasks(cases,stats,dict(A=1,B=0,C=0))
        self.assertEqual(chosen[0]['id'],'2')

    def test_yaml_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            p=Path(temporary)/'config.yaml'
            p.write_text('scheduler:\n  quotas: {A: 1, B: 2, C: 1}\n')
            self.assertEqual(load_settings(p)['quotas'],dict(A=1,B=2,C=1))
            for content in ('scheduler: {repeats_per_task: true}', 'scheduler: {quotas: {A: 0, B: 0, C: 0}}',
                            'scheduler: {cold_start_repeats: 0}', 'scheduler: {unknown: 2}'):
                p.write_text(content)
                with self.assertRaises(ValueError):load_settings(p)

    def test_two_stages_in_order_with_disjoint_evidence_and_no_generic_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary);initial=snapshot(ROOT,output/'initial');seen=[]
            def propose(source,candidate,artifacts,model,timeout,stage,materials,history,remaining):
                index=json.loads((history/'index.json').read_text())
                tasks={x['task_id'] for x in index['tasks']}
                expected={'b_stable'} if stage=='stability_repair' else {'a_forget','b_forget'}
                self.assertEqual(tasks,expected)
                self.assertEqual({p.name for p in (history/'tasks').iterdir()},expected)
                all_text='\n'.join(p.read_text() for p in history.rglob('*') if p.is_file())
                for name in {c['id'] for c in self.suite.cases}-expected:
                    self.assertNotIn('TASK_TRACE:'+name,all_text)
                self.assertNotIn('DO_NOT_EXPOSE_VERIFIER',all_text)
                self.assertNotIn('DO_NOT_EXPOSE_REQUEST',all_text)
                self.assertTrue((history/'current_harness/task_agent.py').is_file())
                self.assertTrue((history/'comparison.md').is_file())
                for item in index['tasks']:
                    task_dir=history/'tasks'/item['task_id']
                    self.assertTrue((task_dir/'task.json').is_file())
                    self.assertTrue((task_dir/'comparison.md').is_file())
                    self.assertEqual(len(item['current']['trials']),4)
                    for trial in item['current']['trials']:
                        trace=Path(trial['trajectory'])
                        self.assertEqual(trace.parent.name,'success' if trial['passed'] else 'failure')
                        self.assertTrue((trace/'trajectory.md').is_file())
                        self.assertTrue((trace/'result.json').is_file())
                        self.assertEqual(json.loads((trace/'outcome.json').read_text())['passed'],trial['passed'])
                if stage=='stability_repair':
                    self.assertNotIn('previous',index['tasks'][0])
                    self.assertFalse(any(history.glob('tasks/*/previous_harness')))
                    self.assertEqual({x['passed'] for x in index['tasks'][0]['current']['trials']},{True,False})
                else:
                    self.assertTrue((source/'harness/task/prompt.py').read_text().endswith('# stability_repair\n'))
                    self.assertFalse((history/'generation.diff').exists())
                    for item in index['tasks']:
                        self.assertIn('previous',item)
                        self.assertEqual(len(item['previous']['trials']),4)
                        self.assertTrue(Path(item['harness_diff']).is_file())
                        self.assertTrue((Path(item['previous']['harness'])/'task_agent.py').is_file())
                snapshot(source,candidate)
                with (candidate/'harness/task/prompt.py').open('a') as f:f.write('# '+stage+'\n')
                seen.append(stage)
                return candidate
            with patch.object(evolution,'ExecutionPool',FixturePool),patch.object(evolution,'check'),patch.object(evolution,'propose',side_effect=propose):
                state=evolution.evolve(output,initial,self.suite,{},10,2,None,self.settings,2)
                self.assertEqual(seen,['stability_repair','forgetting_repair'])
                self.assertEqual(state['round'],2)
                self.assertEqual(len(FixturePool.calls),48)
                self.assertEqual({v['evaluation_count'] for v in state['tasks'].values()},{8})
                self.assertEqual(state['tasks']['a_forget']['label'],'C')
                self.assertEqual(state['tasks']['b_stable']['correct_count'],3)
                self.assertEqual(state['tasks']['b_improve']['label'],'A')
                before=len(FixturePool.calls)
                evolution.evolve(output,initial,self.suite,{},10,2,state,self.settings,2)
                self.assertEqual(len(FixturePool.calls),before)
                evolution.validate_state(state,initial)

    def test_cold_start_only_and_error_resume_preserves_completed_trials(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary);initial=snapshot(ROOT,output/'initial')
            FixturePool.failure=(1,2,'a_forget')
            with patch.object(evolution,'ExecutionPool',FixturePool),patch.object(evolution,'check'),patch.object(evolution,'propose') as propose:
                with self.assertRaisesRegex(RuntimeError,'execution errors'):
                    evolution.evolve(output,initial,self.suite,{},10,1,None,self.settings,2)
                self.assertFalse((output/'state.json').exists())
                FixturePool.failure=None
                state=evolution.evolve(output,initial,self.suite,{},10,1,None,self.settings,2)
                propose.assert_not_called()
                counts=Counter(FixturePool.calls)
                self.assertEqual(counts[(1,1,'a_forget')],1)
                self.assertEqual(counts[(1,2,'a_forget')],2)
                self.assertEqual(counts[(1,2,'b_stable')],1)
                self.assertEqual(state['tasks']['a_forget']['evaluation_count'],5)
                self.assertEqual(state['tasks']['a_forget']['valid_evaluation_count'],4)
                self.assertEqual(state['tasks']['a_forget']['error_count'],1)
                self.assertEqual(state['tasks']['a_forget']['label'],'A')

    def test_resume_after_stability_does_not_repeat_evaluation_or_completed_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary);initial=snapshot(ROOT,output/'initial');stages=[]
            def propose(source,candidate,artifacts,model,timeout,stage,*args):
                stages.append(stage)
                if stage=='forgetting_repair':raise RuntimeError('interrupted Meta')
                return snapshot(source,candidate)
            with patch.object(evolution,'ExecutionPool',FixturePool),patch.object(evolution,'check'),patch.object(evolution,'propose',side_effect=propose):
                with self.assertRaisesRegex(RuntimeError,'interrupted Meta'):
                    evolution.evolve(output,initial,self.suite,{},10,2,None,self.settings,2)
            committed=json.loads((output/'state.json').read_text());self.assertEqual(committed['round'],1)
            calls=len(FixturePool.calls)
            def finish(source,candidate,*args):return snapshot(source,candidate)
            with patch.object(evolution,'ExecutionPool',FixturePool),patch.object(evolution,'propose',side_effect=finish) as resumed:
                state=evolution.evolve(output,initial,self.suite,{},10,2,committed,self.settings,2)
                self.assertEqual(resumed.call_count,1)
                self.assertEqual(state['round'],2)
                self.assertEqual(len(FixturePool.calls),calls)

    def test_killed_batch_recovers_individual_task_receipts(self):
        class InterruptedPool(FixturePool):
            def evaluate_trials(inner, workspace, jobs, model, timeout, suite):
                super().evaluate_trials(workspace, jobs[:2], model, timeout, suite)
                raise RuntimeError('batch killed after two tasks')
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary);initial=snapshot(ROOT,output/'initial')
            with patch.object(evolution,'ExecutionPool',InterruptedPool),patch.object(evolution,'check'):
                with self.assertRaisesRegex(RuntimeError,'batch killed'):
                    evolution.evolve(output,initial,self.suite,{},10,1,None,self.settings,2)
            with patch.object(evolution,'ExecutionPool',FixturePool),patch.object(evolution,'check'):
                state=evolution.evolve(output,initial,self.suite,{},10,1,None,self.settings,2)
            self.assertEqual(len(FixturePool.calls),24)
            self.assertEqual({x['evaluation_count'] for x in state['tasks'].values()},{4})


if __name__=='__main__':unittest.main()

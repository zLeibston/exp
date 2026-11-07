"""Concurrency properties: later repeats start while an earlier trial is blocked."""
from pathlib import Path
from collections import Counter
import sys
import tempfile
from threading import Event, Lock
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tasks import TaskSuite
from pipeline.execution_pool import ExecutionPool
from pipeline.scheduling import repeated_evaluation


class TrialPipelineTests(unittest.TestCase):
    def test_cached_trials_bypass_build_queue_and_repeats_share_preparation(self):
        cached_ran, building = Event(), Event()
        prepared, builds, calls = set(), [], []
        active = maximum = 0
        lock = Lock()
        def prepare(case, directory, model):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                builds.append(case['id'])
                building.set()
                if not cached_ran.wait(3):
                    raise RuntimeError('Cached trial was blocked behind preparation')
                prepared.add(case['id'])
            finally:
                with lock: active -= 1
        def run(workspace, directory, model, timeout, inputs):
            name = inputs['id']
            calls.append(name)
            if name == 'cached':
                self.assertTrue(building.wait(3))
                cached_ran.set()
            else:
                self.assertIn(name, prepared)
            return dict(prediction='ok', history=[])
        suite = TaskSuite(name='fixture', cases=[dict(id=x, inputs=dict(id=x))
                          for x in ('slow1', 'slow2', 'cached')], task_runner=run,
                          grade_case=lambda *args: dict(passed=True, error=None),
                          environment_ready=lambda case: case['id']=='cached', prepare_environment=prepare)
        with tempfile.TemporaryDirectory() as tmp, ExecutionPool({}, workers=1) as pool:
            observations = repeated_evaluation(pool, ROOT, Path(tmp)/'evaluation', {}, 10, suite, 4)
        self.assertEqual(builds, ['slow1', 'slow2'])
        self.assertEqual(maximum, 1)
        self.assertEqual(Counter(calls), dict(slow1=4, slow2=4, cached=4))
        self.assertTrue(all(len(trials)==4 for trials in observations.values()))

    def test_preparation_error_preserves_other_receipts_and_resume(self):
        fail = True
        builds, calls = [], []
        def prepare(case, directory, model):
            builds.append(case['id'])
            if fail: raise RuntimeError('fixture build failed')
        def run(workspace, directory, model, timeout, inputs):
            calls.append(inputs['id'])
            return dict(prediction='ok', history=[])
        suite = TaskSuite(name='fixture', cases=[dict(id=x, inputs=dict(id=x)) for x in ('missing','cached')],
                          task_runner=run, grade_case=lambda *args: dict(passed=True,error=None),
                          environment_ready=lambda case: case['id']=='cached', prepare_environment=prepare)
        with tempfile.TemporaryDirectory() as tmp, ExecutionPool({},workers=1) as pool:
            folder=Path(tmp)/'evaluation'
            with self.assertRaisesRegex(RuntimeError,'execution errors'):
                repeated_evaluation(pool,ROOT,folder,{},10,suite,4)
            self.assertEqual(calls,['cached']*4)
            fail=False
            observations=repeated_evaluation(pool,ROOT,folder,{},10,suite,4)
            self.assertEqual(builds,['missing','missing'])
            self.assertEqual(Counter(calls),dict(cached=4,missing=4))
            self.assertEqual(sum(t['error'] is None for t in observations['missing']),4)
            self.assertEqual(len(observations['missing']),8)

    def test_agent_failures_are_valid_misses_and_are_not_retried(self):
        calls = []
        def run(workspace, directory, model, timeout, inputs):
            calls.append(directory)
            return dict(prediction=None, history=[], task_failure=dict(reason='agent_error'))
        suite = TaskSuite(name='fixture', cases=[dict(id='task', inputs={})], task_runner=run,
                          grade_case=lambda *args: dict(passed=False, error=None))
        with tempfile.TemporaryDirectory() as tmp, ExecutionPool({}, workers=2) as pool:
            folder = Path(tmp)/'evaluation'
            first = repeated_evaluation(pool, ROOT, folder, {}, 10, suite, 4)
            self.assertTrue(all(not t['passed'] and t['error'] is None for t in first['task']))
            self.assertEqual(repeated_evaluation(pool, ROOT, folder, {}, 10, suite, 4), first)
            self.assertEqual(len(calls), 4)

    def test_slow_first_repeat_does_not_block_later_repeats(self):
        later_started, slow_finished = Event(), Event()
        lock = Lock()
        running, maximum = 0, 0
        directories = []
        def run(workspace, directory, model, timeout, inputs):
            nonlocal running, maximum
            repeat = int(directory.parent.parent.name[7:])
            with lock:
                running += 1
                maximum = max(maximum, running)
                directories.append(directory)
            try:
                if inputs['id'] == 'slow' and repeat == 1:
                    if not later_started.wait(5):
                        raise RuntimeError('Repeat barrier prevented the later trial from starting')
                    slow_finished.set()
                if inputs['id'] == 'fast' and repeat == 2:
                    self.assertFalse(slow_finished.is_set())
                    later_started.set()
                return dict(prediction='ok', history=[])
            finally:
                with lock:running -= 1
        suite=TaskSuite(name='fixture',cases=[dict(id=x,inputs=dict(id=x)) for x in ('slow','fast')],
                        task_runner=run,grade_case=lambda *args:dict(passed=True,error=None))
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with ExecutionPool({},workers=2) as pool:
                results=repeated_evaluation(pool,ROOT,root/'evaluation',{},10,suite,4)
                count=len(directories)
                again=repeated_evaluation(pool,ROOT,root/'evaluation',{},10,suite,4)
            self.assertEqual(results,again)
            self.assertEqual(len(directories),count)
            self.assertEqual(len(set(directories)),8)
            self.assertTrue(all((p/'evaluation.json').is_file() for p in directories))
        self.assertTrue(later_started.is_set())
        self.assertTrue(slow_finished.is_set())
        self.assertEqual(maximum,2)
        self.assertEqual([len(v) for v in results.values()],[4,4])

    def test_error_in_one_repeat_does_not_drop_other_queued_trials(self):
        calls=[]
        fail=True
        def run(workspace,directory,model,timeout,inputs):
            repeat=int(directory.parent.parent.name[7:]);calls.append(repeat)
            if fail and repeat==2:raise RuntimeError('fixture failure')
            return dict(prediction='ok',history=[])
        suite=TaskSuite(name='fixture',cases=[dict(id='task',inputs={})],task_runner=run,
                        grade_case=lambda *args:dict(passed=True,error=None))
        with tempfile.TemporaryDirectory() as tmp, ExecutionPool({},workers=2) as pool:
            folder=Path(tmp)/'evaluation'
            with self.assertRaisesRegex(RuntimeError,'execution errors'):
                repeated_evaluation(pool,ROOT,folder,{},10,suite,4)
            self.assertEqual(sorted(calls),[1,2,3,4])
            fail=False
            results=repeated_evaluation(pool,ROOT,folder,{},10,suite,4)
            self.assertEqual(sorted(calls),[1,2,2,3,4])
            self.assertEqual(len(results['task']),5)
            self.assertEqual(sum(t['error'] is None for t in results['task']),4)
            self.assertEqual(results,repeated_evaluation(pool,ROOT,folder,{},10,suite,4))
            self.assertEqual(len(calls),5)


if __name__=='__main__':unittest.main()

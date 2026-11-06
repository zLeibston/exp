"""Concurrency properties: later repeats start while an earlier trial is blocked."""
from pathlib import Path
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

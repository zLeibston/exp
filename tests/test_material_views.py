"""Per-task evidence organization, exact historical versions, and prompt guidance."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pipeline.materials import build_view,export_view
from pipeline.storage import snapshot,harness_digest,inventory,write
from harness.meta.prompt import improve_instruction


class MaterialViewTests(unittest.TestCase):
    def test_each_regression_uses_its_actual_historical_harness_and_every_trial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            versions={}
            for label in ('old_a','old_b','current','candidate'):
                versions[label]=snapshot(ROOT,root/label)
                (versions[label]/'harness/task/version_marker.py').write_text('version = '+repr(label)+'\n')
            def observation(identifier,name,version,passed,round_index):
                trials=[]
                for repeat,result in enumerate(passed,1):
                    trace=root/'raw'/identifier/name/f'repeat_{repeat:02d}';trace.mkdir(parents=True)
                    history=[dict(role='user',text='TASK:'+identifier+':'+name),
                             dict(role='assistant',text='analysis',tool_calls=[dict(id='call1',name='bash',arguments=json.dumps(dict(command='echo first\necho second')))],native=dict(reasoning_content='RAW_HISTORY_RETAINED')),
                             dict(role='tool',name='bash',call_id='call1',text='first\nsecond\n'),
                             dict(role='assistant',text='done')]
                    write(trace/'result.json',dict(prediction='done',history=history))
                    (trace/'verifier.log').write_text('PRIVATE_VERIFIER')
                    (trace/'request.json').write_text('PRIVATE_REQUEST')
                    trials.append(dict(repeat=repeat,passed=result,error=None,trajectory=str(trace)))
                return dict(round=round_index,workspace=str(versions[version]),harness_hash=harness_digest(versions[version]),correct=sum(passed),evaluations=4,attempts=trials)
            cases=[dict(id=x,inputs=dict(problem=x)) for x in ('a','b','stable')]
            transitions=[]
            for identifier,before,after,version,old_round,previous,current in (
                ('a','A','B','old_a',1,[True]*4,[True,False,False,False]),
                ('b','B','C','old_b',5,[True,False,True,False],[False]*4),
                ('stable','B','B','old_a',1,[True,False,False,False],[True,False,True,False])):
                transitions.append(dict(task_id=identifier,before=before,after=after,stage='stability_repair' if identifier=='stable' else 'forgetting_repair',
                     previous=observation(identifier,'previous',version,previous,old_round),
                     current=observation(identifier,'current','current',current,9),correct=sum(current),evaluations=4))
            for stage,expected in [('stability_repair',{'stable'}),('forgetting_repair',{'a','b'})]:
                view=build_view(root/'views'/stage,stage,cases,transitions,versions['current'],versions['candidate'])
                index=json.loads((view/'index.json').read_text())
                self.assertEqual({i['task_id'] for i in index['tasks']},expected)
                self.assertEqual(inventory(view/'current_harness'),inventory(versions['current']/'harness/task'))
                self.assertFalse(any(view.rglob('verifier.log')))
                self.assertFalse(any(view.rglob('request.json')))
                self.assertFalse((view/'harnesses').exists())
                for item in index['tasks']:
                    self.assertNotIn('before',item)
                    self.assertNotIn('after',item)
                    path=view/'tasks'/item['task_id']
                    self.assertNotIn('Transition:',(path/'comparison.md').read_text())
                    self.assertIn('| Repeat | Previous | Current |',(path/'comparison.md').read_text())
                    self.assertEqual(len(item['current']['trials']),4)
                    if stage=='forgetting_repair':
                        old='old_a' if item['task_id']=='a' else 'old_b'
                        self.assertEqual(inventory(path/'previous_harness'),inventory(versions[old]/'harness/task'))
                        difference=(path/'harness.diff').read_text()
                        self.assertIn("-version = '"+old+"'",difference)
                        self.assertIn("+version = 'candidate'",difference)
                        evaluated=(path/'evaluated.diff').read_text()
                        self.assertIn("+version = 'current'",evaluated)
                        self.assertNotIn("+version = 'candidate'",evaluated)
                        self.assertEqual(len(item['previous']['trials']),4)
                    else:
                        self.assertFalse((path/'previous').exists())
                        self.assertFalse((path/'previous_harness').exists())
                    for name in ('previous','current'):
                        for trial in item.get(name,{}).get('trials',[]):
                            trace=Path(trial['trajectory'])
                            raw=json.loads((trace/'result.json').read_text())
                            self.assertEqual(len(raw['history']),4)
                            self.assertEqual(raw['history'][1]['native']['reasoning_content'],'RAW_HISTORY_RETAINED')
                            rendered=(trace/'trajectory.md').read_text()
                            self.assertIn('echo first\necho second',rendered)
                            self.assertIn('Turn 04: assistant',rendered)
                            self.assertEqual(trace.parent.name,'success' if trial['passed'] else 'failure')
                self.assertEqual(len(list(view.rglob('trajectory.md'))),4 if stage=='stability_repair' else 16)
                # Even accidentally added host files must not enter the tool view.
                (view/'labels.json').write_text('PRIVATE_SCHEDULER')
                (view/'tasks/unselected').mkdir()
                (view/'tasks/unselected/trace.log').write_text('PRIVATE_OTHER_TASK')
                private_index=json.loads((view/'index.json').read_text())
                for item in private_index['tasks']:
                    item['current']['scheduler_seed']='PRIVATE_SEED'
                write(view/'index.json',private_index)
                public=root/('public-'+stage)
                export_view(view,public)
                self.assertFalse((public/'labels.json').exists())
                self.assertFalse((public/'tasks/unselected').exists())
                guest=json.loads((public/'index.json').read_text())
                self.assertNotIn('stage',guest)
                self.assertEqual(guest['current_harness'],'/materials/current_harness')
                self.assertTrue(guest['candidate_differs_from_evaluated'])
                self.assertNotIn(str(root),json.dumps(guest))
                for item in guest['tasks']:
                    self.assertNotIn('round',item['current'])
                    self.assertNotIn('scheduler_seed',item['current'])
                    if stage=='forgetting_repair':
                        delta=public/Path(item['evaluated_diff']).relative_to('/materials')
                        self.assertIn("+version = 'current'",delta.read_text())
                    for trial in item['current']['trials']:
                        trace=public/Path(trial['trajectory']).relative_to('/materials')
                        self.assertTrue((trace/'trajectory.md').is_file())
                        outcome=json.loads((trace/'outcome.json').read_text())
                        self.assertEqual(set(outcome),{'repeat','passed'})
                # Rebuilding the same mount source must remove the previous task set.
                remaining=[entry for entry in transitions if entry['task_id']==sorted(expected)[0]]
                build_view(view,stage,cases,remaining,versions['current'])
                self.assertEqual({path.name for path in (view/'tasks').iterdir()},{sorted(expected)[0]})
                self.assertFalse((view/'labels.json').exists())
                self.assertFalse(any(view.rglob('evaluated.diff')))
            broken=transitions[0]
            broken['current']['harness_hash']='stale-harness'
            with self.assertRaisesRegex(ValueError,'changed since its evaluation'):
                build_view(root/'broken','forgetting_repair',cases,[broken],versions['current'])
            broken['current']['harness_hash']=harness_digest(versions['current'])
            Path(broken['current']['attempts'][0]['trajectory'],'result.json').unlink()
            with self.assertRaisesRegex(ValueError,'Missing trajectory'):
                build_view(root/'broken','forgetting_repair',cases,[broken],versions['current'])

    def test_prompt_explains_the_selected_stage_layout(self):
        for stage in ('stability_repair','forgetting_repair'):
            text=improve_instruction('/candidate','/materials',2,stage=stage)
            self.assertIn('Material directory guide:',text)
            self.assertIn('current_harness/',text)
            self.assertIn('trajectory.md',text)
            for fragment in ('B-to-B','A to B','B/C','Remaining outer rounds'):
                self.assertNotIn(fragment,text)
            if stage=='stability_repair':self.assertNotIn('previous_harness/',text)
            else:
                self.assertIn('previous_harness/',text)
                self.assertIn('harness.diff',text)
                self.assertIn('have not been re-evaluated',text)


if __name__=='__main__':unittest.main()

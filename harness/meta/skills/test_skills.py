"""Offline checks for mandatory selection, lazy references and tool-loop retention.

Run from exp/: python -m unittest harness.meta.skills.test_skills -v
"""
import importlib.util
import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import unittest
from unittest.mock import patch

from harness.meta.improve_agent import ImproveAgent
from harness.meta.skills import STAGE_SKILLS, load_skill


class MetaSkillTests(unittest.TestCase):
    def test_model_budget_reserves_final_call_and_records_reason(self):
        from harness.meta.agent.llm_withtools import chat_with_agent
        requests = []
        def respond(msg, msg_history, tools, **kwargs):
            requests.append(tools)
            history = [*msg_history, dict(role='user', text=msg)]
            if len(requests) == 1:
                text, calls = '', [dict(id='a', name='bash', arguments='{"command":"true"}')]
            else:
                self.assertEqual(tools, [])
                self.assertIn('Final model call', msg)
                text, calls = '{"response":"done"}', []
            history.append(dict(role='assistant', text=text, tool_calls=calls, native={'marker':'retained'}))
            return text, history, dict(tool_calls=calls)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'meta_state.json'
            with patch('harness.meta.agent.llm_withtools.MAX_MODEL_CALLS', 2), \
                    patch('harness.meta.agent.llm_withtools.get_response_from_llm', side_effect=respond), \
                    patch('harness.meta.agent.llm_withtools.process_tool_call', return_value='ok') as execute:
                chat_with_agent('repair', tools_available='all', finish_on_budget=True,
                                state_path=path, logging=lambda *args:None)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['status'], 'completed')
            self.assertEqual(saved['stop_reason'], 'model_budget')
            self.assertEqual(saved['model_calls'], 2)
            execute.assert_called_once()

    def test_api_failure_keeps_native_history_and_pending_request(self):
        from harness.meta.agent.llm_withtools import chat_with_agent
        history = [dict(role='assistant', text='earlier', native={'marker':'retained'})]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'meta_state.json'
            with patch('harness.meta.agent.llm_withtools.get_response_from_llm', side_effect=RuntimeError('fixture')):
                with self.assertRaises(RuntimeError):
                    chat_with_agent('next request', msg_history=history, state_path=path, logging=lambda *args:None)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['status'], 'failed')
            self.assertEqual(saved['history'][0]['native'], {'marker':'retained'})
            self.assertIn('next request', saved['pending_message'])

    def test_final_only_tool_request_is_failed_without_execution(self):
        from harness.meta.agent.llm_withtools import chat_with_agent
        def respond(msg, msg_history, **kwargs):
            calls = [dict(id='a', name='bash', arguments='{"command":"true"}')]
            return '', [dict(role='assistant', text='', tool_calls=calls)], dict(tool_calls=calls)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'meta_state.json'
            with patch('harness.meta.agent.llm_withtools.MAX_MODEL_CALLS', 1), \
                    patch('harness.meta.agent.llm_withtools.get_response_from_llm', side_effect=respond), \
                    patch('harness.meta.agent.llm_withtools.process_tool_call') as execute:
                with self.assertRaisesRegex(RuntimeError, 'final-only'):
                    chat_with_agent('repair', tools_available='all', finish_on_budget=True,
                                    state_path=path, logging=lambda *args:None)
            execute.assert_not_called()
            saved = json.loads(path.read_text())
            self.assertEqual(saved['status'], 'failed')
            self.assertTrue(saved['history'][-1]['is_error'])

    def test_budget_overflow_keeps_all_results_then_requests_tool_free_summary(self):
        from harness.meta.agent.llm_withtools import chat_with_agent
        requests = []

        def respond(msg, msg_history, tools, **kwargs):
            requests.append(tools)
            history = [*msg_history, dict(role='user', text=msg)]
            if len(requests) == 1:
                calls = [dict(id=str(i), name='bash', arguments='{"command":"true"}')
                         for i in range(2)]
                text = ''
            else:
                self.assertEqual(tools, [])
                results = [item for item in history if item['role'] == 'tool']
                self.assertEqual([item['call_id'] for item in results], ['0', '1'])
                self.assertFalse(results[0]['is_error'])
                self.assertTrue(results[1]['is_error'])
                self.assertIn('Tool calls remaining: 0', msg)
                text, calls = '{"response":"budget reached; checks unfinished"}', []
            history.append(dict(role='assistant', text=text, tool_calls=calls))
            return text, history, dict(tool_calls=calls)

        with patch('harness.meta.agent.llm_withtools.get_response_from_llm', side_effect=respond), \
                patch('harness.meta.agent.llm_withtools.process_tool_call', return_value='ok') as execute:
            history = chat_with_agent('repair', tools_available='all', max_tool_calls=1,
                                      finish_on_budget=True, logging=lambda *args: None)
        execute.assert_called_once()
        self.assertEqual(len(requests), 2)
        self.assertIn('checks unfinished', history[-1]['text'])

    def test_selected_entrypoint_is_mandatory_and_references_are_lazy(self):
        for stage, name in STAGE_SKILLS.items():
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                agent = ImproveAgent(chat_history_file=str(Path(tmp)/'chat.log'))
                path, entrypoint = load_skill(stage)
                with patch('harness.meta.improve_agent.chat_with_agent', return_value=[]) as chat:
                    agent.forward('/candidate/harness/task', '/materials', stage=stage,
                                  instruction='Caller-supplied instructions')
                instruction = chat.call_args.args[0]
                self.assertIn('Caller-supplied instructions', instruction)
                self.assertIn('Candidate harness to modify: `/candidate/harness/task`', instruction)
                self.assertIn('Read-only materials root: `/materials`', instruction)
                self.assertEqual(instruction.count(entrypoint), 1)
                self.assertIn(str(path.parent), instruction)
                other = next(value for value in STAGE_SKILLS.values() if value != name)
                self.assertNotIn(f'name: {other}\n', instruction)
                references = re.findall(r'\]\((references/[^)]+)\)', entrypoint)
                self.assertEqual(len(references), 6)
                for relative in references:
                    content = (path.parent/relative).read_text()
                    self.assertTrue(content.strip())
                    self.assertNotIn(content.strip(), instruction)

    def test_skill_and_read_reference_survive_a_real_bash_tool_turn(self):
        for stage in STAGE_SKILLS:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                path, entrypoint = load_skill(stage)
                relative = re.findall(r'\]\((references/[^)]+)\)', entrypoint)[0]
                reference = path.parent/relative
                requests = []

                def respond(msg, msg_history, **kwargs):
                    requests.append(msg)
                    history = [*msg_history, dict(role='user', text=msg)]
                    calls = [] if msg_history else [dict(id='read-reference', name='bash',
                        arguments=json.dumps(dict(command='cat '+shlex.quote(str(reference)))))]
                    if msg_history:
                        self.assertEqual(history[0]['text'].count(entrypoint), 1)
                        result = next(item for item in history if item.get('call_id')=='read-reference')
                        self.assertFalse(result['is_error'])
                        self.assertIn(reference.read_text().strip(), result['text'])
                    text = '' if calls else '{"response":"checked"}'
                    history.append(dict(role='assistant', text=text, tool_calls=calls))
                    return text, history, dict(tool_calls=calls)

                with patch('harness.meta.agent.llm_withtools.get_response_from_llm', side_effect=respond):
                    history = ImproveAgent(chat_history_file=str(Path(tmp)/'chat.log')).forward(
                        '/candidate/harness/task', '/materials', stage=stage,
                        context_paths=['/materials/index.json'])
                self.assertEqual(len(requests), 2)
                first = requests[0]
                self.assertLess(first.index('Candidate harness to modify:'), first.index(entrypoint))
                self.assertLess(first.index(entrypoint), first.index('Finish with exactly one JSON object:'))
                self.assertNotIn(entrypoint, requests[1])
                self.assertEqual(history[-1]['text'], '{"response":"checked"}')

    def test_skill_paths_follow_copied_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'engine/harness/meta/skills'
            shutil.copytree(Path(__file__).parent, target, ignore=shutil.ignore_patterns('__pycache__'))
            spec = importlib.util.spec_from_file_location('relocated_skills', target/'__init__.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for stage in STAGE_SKILLS:
                path, _ = module.load_skill(stage)
                self.assertTrue(path.is_relative_to(target))
                self.assertTrue(all((path.parent/relative).is_file() for relative in
                    re.findall(r'\]\((references/[^)]+)\)', path.read_text())))

    def test_unknown_stage_cannot_bypass_skill_with_custom_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = ImproveAgent(chat_history_file=str(Path(tmp)/'chat.log'))
            with patch('harness.meta.improve_agent.chat_with_agent') as chat:
                with self.assertRaises(ValueError):
                    agent.forward('/candidate', '/materials', stage='general', instruction='override')
                chat.assert_not_called()


if __name__ == '__main__':
    unittest.main()

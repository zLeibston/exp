"""Real tmux/PTY behavior without model calls or shared trial state."""
from pathlib import Path
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness.task.agent.tools import bash


@unittest.skipUnless(shutil.which('tmux'), 'tmux is required')
class PersistentTerminalTests(unittest.TestCase):
    def setUp(self):
        self.session=bash.BashSession()
    def tearDown(self):
        self.session.stop()

    def test_cwd_environment_and_tty_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.session.perform('cd '+shlex.quote(tmp)+'; export RSI_PERSIST=value')
            output=self.session.perform('printf "%s:%s" "$PWD" "$RSI_PERSIST"; test -t 0 && shopt -q login_shell')
            self.assertIn(tmp+':value',output)
            self.assertIn('exit_code: 0',output)

    def test_wait_does_not_kill_command_and_poll_completes(self):
        output=self.session.perform('sleep 0.3; printf finished',wait_seconds=0)
        self.assertIn('state: running',output)
        with self.assertRaisesRegex(ValueError,'still running'):
            self.session.perform('echo must-not-run')
        output=self.session.perform('',action='poll',wait_seconds=2)
        self.assertIn('finished',output)
        self.assertIn('state: completed',output)

    def test_interactive_input_and_interrupt(self):
        code='value=input("value? "); print("received="+value)'
        output=self.session.perform('python3 -c '+shlex.quote(code),wait_seconds=.1)
        self.assertIn('state: running',output)
        self.assertIn('received=hello',self.session.perform('hello\n',action='input'))
        self.session.perform('sleep 30; echo must-not-continue',wait_seconds=.1)
        output=self.session.perform('C-c',action='keys')
        self.assertIn('exit_code: 130',output)
        self.assertNotIn('must-not-continue',output)
        self.assertIn('alive',self.session.perform('echo alive'))

    def test_multiline_and_large_commands_preserve_quoting(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'result.txt'
            text="quote ' and double \" and 中文\n"+'x'*20000
            command="python3 - <<'PY'\nfrom pathlib import Path\nPath("+repr(str(path))+").write_text("+repr(text)+")\nPY"
            self.assertIn('exit_code: 0',self.session.perform(command))
            self.assertEqual(path.read_text(),text)

    def test_separate_sessions_do_not_share_shell_state(self):
        other=bash.BashSession()
        try:
            self.session.perform('export RSI_ISOLATED=private')
            self.assertIn('unset',other.perform('printf "%s" "${RSI_ISOLATED-unset}"'))
        finally:other.stop()

    def test_exit_code_and_explicit_restart(self):
        self.assertIn('exit_code: 7',self.session.perform('exit 7'))
        with self.assertRaisesRegex(ValueError,'Terminal exited'):
            self.session.perform('echo should-not-run')
        with patch.object(bash,'_session',self.session):
            try:self.assertIn('restarted',bash.tool_function('echo restarted',action='restart'))
            finally:bash.reset_session()

    def test_user_prompt_hook_does_not_break_completion(self):
        self.assertIn('exit_code: 1',self.session.perform("PROMPT_COMMAND=':'; false"))
        self.session.perform('sleep 30; echo must-not-continue',wait_seconds=.1)
        output=self.session.perform('C-c',action='keys',wait_seconds=2)
        self.assertIn('state: completed',output)
        self.assertNotIn('must-not-continue',output)
        self.assertIn('still-alive',self.session.perform('echo still-alive'))

    def test_native_tool_loop_and_terminal_survive_worker_exit(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);work=directory/'work';work.mkdir()
            actions=[dict(command='cd '+shlex.quote(str(work))+'; read -p "value? " RSI_VALUE; export RSI_STATE=kept',wait_seconds=.1),
                     dict(command='hello\n',action='input'),
                     dict(command='printf "%s:%s:%s" "$RSI_VALUE" "$RSI_STATE" "$PWD"; test -t 0')]
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_POST(self):
                    body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    step=sum(m['role']=='tool' for m in body['messages'])
                    if step<len(actions):
                        message=dict(role='assistant',content=None,tool_calls=[dict(id='call'+str(step),type='function',
                                     function=dict(name='bash',arguments=json.dumps(actions[step])))])
                    else:message=dict(role='assistant',content='{"response":"done"}')
                    data=json.dumps(dict(choices=[dict(message=message,finish_reason='stop')])).encode()
                    self.send_response(200);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                config=directory/'api.json'
                config.write_text(json.dumps(dict(task_agent=dict(model='fixture',provider='openai',api_key='fixture',
                                      url=f'http://127.0.0.1:{server.server_port}',retries=0),
                                      meta_agent=dict(model='unused',api_key='unused'))))
                output=directory/'output';output.mkdir()
                request=directory/'request.json'
                request.write_text(json.dumps(dict(kind='task',workspace=str(root),runtime_workspace=str(root),
                    model=dict(config_path=str(config)),inputs=dict(domain='terminal'),
                    log_path=str(output/'chat.log'),result_path=str(output/'result.json'))))
                result=subprocess.run([sys.executable,str(root/'pipeline/worker.py'),str(request)],cwd=root,
                                      env=dict(os.environ,TMPDIR=tmp),capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
                response=json.loads((output/'result.json').read_text())
                tools=[m['text'] for m in response['history'] if m['role']=='tool']
                self.assertEqual(response['prediction'],'done')
                self.assertIn('state: running',tools[0])
                self.assertIn('hello:kept:'+str(work),tools[2])
                self.assertNotIn('__RSI_TOKEN', ''.join(tools))
                sockets=list(directory.glob('rsi-terminal-*/tmux.sock'))
                self.assertEqual(len(sockets),1)
                self.assertEqual(subprocess.run(['tmux','-S',str(sockets[0]),'has-session'],capture_output=True).returncode,0)
            finally:
                server.shutdown();server.server_close();thread.join()
                for socket in directory.glob('rsi-terminal-*/tmux.sock'):
                    subprocess.run(['tmux','-S',str(socket),'kill-server'],capture_output=True,timeout=5)

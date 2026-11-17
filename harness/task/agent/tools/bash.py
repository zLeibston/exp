"""One persistent tmux terminal per Task worker; the container owns its lifetime."""
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

from harness.task.agent.trace import annotate

MAX_OUTPUT_CHARS = 50_000
_session = None


def tool_info():
    return {
        'name': 'bash',
        'description': '''Run commands in a persistent Bash terminal. Directory, environment and processes persist across calls.
Actions: run a command; poll with an empty command to wait; input sends text (include \\n to submit); keys sends keys such as C-c; restart opens a fresh shell and clears shell state.
While running, use poll/input/keys instead of run. wait_seconds defaults to 10 (max 120); waiting does not kill the command.
Returns terminal state, exit code when known, and merged stdout/stderr. Output is bounded; redirect verbose output to files.''',
        'input_schema': {
            'type': 'object',
            'properties': {
                'command': {'type': 'string', 'description': 'Shell command, literal input, terminal keys, or empty for polling.'},
                'action': {'type': 'string', 'enum': ['run', 'poll', 'input', 'keys', 'restart']},
                'wait_seconds': {'type': 'number', 'description': 'Seconds to wait, from 0 to 120; default 10.'},
            },
            'required': ['command'],
        },
    }


class BashSession:
    def __init__(self):
        self.directory = Path(tempfile.mkdtemp(prefix='rsi-terminal-'))
        self.socket = self.directory / 'tmux.sock'
        self.status = self.directory / 'status'
        self.script = self.directory / 'command.sh'
        self.prompt = '__RSI_PROMPT_' + uuid.uuid4().hex + '__'
        self.end = '__RSI_END_' + uuid.uuid4().hex + '__'
        self.history_marker = self.end + '_history'
        self.scrollback_clipped = False
        self.token = None
        self.running = False
        self.interrupted = False
        self.started = False
        self.screen = ''
        self.echo = ''
        self.exit_code = 0

    def tmux(self, *args, input=None):
        result = subprocess.run(['tmux', '-S', str(self.socket), '-f', '/dev/null', *args],
                                input=input, capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError('Terminal control failed: ' + result.stderr.strip())
        return result.stdout

    def start(self):
        if self.started:
            return
        configuration = self.directory / 'tmux.conf'
        configuration.write_text('set -g history-limit 2000\nset -g status off\nsetw -g remain-on-exit on\n')
        prompt_command = 'printf "%s:%s\\n" "${__RSI_TOKEN:-initial}" "$?" > ' + shlex.quote(str(self.status))
        rc = self.directory / 'bashrc'
        rc.write_text('printf "%s\\n" ' + shlex.quote(self.history_marker) + '\n'
                      + 'PS1=' + shlex.quote(self.prompt) + '\nPS2="> "\nHISTFILE=/dev/null\n'
                      + 'PROMPT_COMMAND=' + shlex.quote(prompt_command) + '\n'
                      + 'trap ' + shlex.quote(prompt_command) + ' EXIT\n')
        result = subprocess.run(['tmux', '-S', str(self.socket), '-f', str(configuration),
                                 'new-session', '-d', '-s', 'task', '-x', '160', '-y', '40',
                                 '-c', os.getcwd(), '/bin/bash --login -i'],
                                env=os.environ.copy(), capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError('Could not start terminal: ' + result.stderr.strip())
        self.started = True
        # Load task login profiles before installing our terminal bookkeeping.
        self.tmux('send-keys', '-t', 'task:0.0', '-l', '--', 'source ' + shlex.quote(str(rc)))
        self.tmux('send-keys', '-t', 'task:0.0', 'Enter')
        deadline = time.monotonic() + 5
        while not self.status.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('Terminal did not reach its initial prompt')
            time.sleep(0.02)
        self.capture()

    def capture(self):
        screen = self.tmux('capture-pane', '-p', '-J', '-S', '-', '-t', 'task:0.0')
        # A startup marker detects history eviction even below the character cap.
        self.scrollback_clipped = self.history_marker not in screen
        screen = screen.replace(self.history_marker, '')
        screen = re.sub(re.escape(self.prompt) + r'(?:[0-9a-f]{32})?', '', screen)
        screen = re.sub(r'__RSI_TOKEN=[0-9a-f]{32}; .*? # ' + re.escape(self.end), '', screen)
        screen = screen.rstrip()
        output = screen[len(self.screen):] if screen.startswith(self.screen) else '[terminal screen refreshed]\n' + screen
        self.screen = screen
        return output.strip()

    def stop(self):
        try:
            if self.started:
                subprocess.run(['tmux', '-S', str(self.socket), 'kill-server'], capture_output=True, timeout=5)
        finally:
            self.started = False
            shutil.rmtree(self.directory, ignore_errors=True)

    def refresh(self):
        state = self.tmux('display-message', '-p', '-t', 'task:0.0', '#{pane_dead} #{pane_dead_status}').split()
        status = self.status.read_text().strip().split(':') if self.status.is_file() else []
        completed = len(status) == 2 and status[0] == self.token and status[1].isdigit()
        if state[0] == '1':
            self.running = False
            self.exit_code = int(status[1]) if completed else int(state[1]) if len(state) > 1 else None
            return True
        if self.running and completed:
            self.exit_code = int(status[1])
            self.running = False
        elif self.running and self.interrupted:
            # Ctrl-C can skip the wrapper; a unique prompt proves the shell is ready
            # even when the task has replaced PROMPT_COMMAND. Do not trap SIGINT:
            # catching it would let interrupted scripts continue executing.
            screen = self.tmux('capture-pane', '-p', '-t', 'task:0.0').rstrip()
            if screen.endswith(self.prompt + self.token):
                status = self.status.read_text().strip().split(':') if self.status.is_file() else []
                completed = len(status) == 2 and status[0] == self.token and status[1].isdigit()
                self.running = False
                self.exit_code = int(status[1]) if completed else None
        return False

    def perform(self, command='', action='run', wait_seconds=10):
        if not isinstance(command, str) or action not in {'run', 'poll', 'input', 'keys'}:
            raise ValueError('Invalid terminal command or action')
        if type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 120:
            raise ValueError('wait_seconds must be between 0 and 120')
        self.start()
        if self.refresh() and action != 'poll':
            raise ValueError('Terminal exited; use action="restart" to start a fresh shell')
        if action == 'run' and not command:
            action = 'poll'
        if action == 'run':
            if self.running:
                raise ValueError('A command is still running; use poll, input, or keys (C-c to interrupt)')
            self.token = uuid.uuid4().hex
            self.interrupted = False
            self.script.write_text(command + '\n')
            self.echo = ('__RSI_TOKEN=' + self.token + '; PS1=' + shlex.quote(self.prompt + self.token)
                         + '; { source ' + shlex.quote(str(self.script))
                         + '; __RSI_CODE=$?; printf "%s:%s\\n" "$__RSI_TOKEN" "$__RSI_CODE" > '
                         + shlex.quote(str(self.status)) + '; (exit "$__RSI_CODE"); } # ' + self.end)
            self.tmux('send-keys', '-t', 'task:0.0', '-l', '--', self.echo)
            self.tmux('send-keys', '-t', 'task:0.0', 'Enter')
            self.running = True
        elif action == 'input':
            if not self.running:
                raise ValueError('No running program; use run for shell commands')
            self.tmux('load-buffer', '-b', 'rsi-input', '-', input=command)
            self.tmux('paste-buffer', '-d', '-b', 'rsi-input', '-t', 'task:0.0')
        elif action == 'keys':
            if not self.running:
                raise ValueError('No running program; use run for shell commands')
            keys = shlex.split(command)
            self.interrupted |= 'C-c' in keys
            self.tmux('send-keys', '-t', 'task:0.0', '--', *keys)
        elif command:
            raise ValueError('poll requires an empty command')
        deadline = time.monotonic() + wait_seconds
        exited = False
        while True:
            exited = self.refresh()
            if not self.running or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if not self.running:
            time.sleep(0.03)  # Let tmux render the command's final PTY bytes.
        output = self.capture()
        clipped = len(output) > MAX_OUTPUT_CHARS
        annotate(terminal_state='exited' if exited else 'running' if self.running else 'completed',
                 exit_code=None if self.running else self.exit_code, stream='pty',
                 output_chars_before_clipping=len(output), output_clipped=clipped or self.scrollback_clipped,
                 scrollback_clipped=self.scrollback_clipped,
                 output_limit_chars=MAX_OUTPUT_CHARS)
        if self.scrollback_clipped:
            output = '[terminal scrollback clipped; earlier output may be unavailable]\n' + output
        if clipped:
            output = '[output clipped; showing the latest terminal output]\n' + output[-MAX_OUTPUT_CHARS:]
        state = 'exited' if exited else 'running' if self.running else 'completed'
        code = 'unknown' if self.running or self.exit_code is None else self.exit_code
        return f'state: {state}\nexit_code: {code}\n{output}'.rstrip()


def reset_session():
    """Explicit cleanup for in-process callers/tests; workers rely on container cleanup."""
    global _session
    session, _session = _session, None
    if session is not None:
        session.stop()


def tool_function(command='', action='run', wait_seconds=10):
    global _session
    try:
        if action == 'restart':
            reset_session()
            action = 'run'
        if _session is None:
            _session = BashSession()
        return _session.perform(command, action, wait_seconds)
    except Exception as exc:
        annotate(error_type=type(exc).__name__)
        return f'Error: {exc}'

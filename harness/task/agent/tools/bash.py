import asyncio
import codecs
import os
import uuid
from harness.task.agent.trace import annotate

MAX_OUTPUT_CHARS = 50_000
CLEANUP_TIMEOUT = 1.0

def tool_info():
    return {
        "name": "bash",
        "description": """Run commands in a bash shell
* Returns the command's exit_code followed by its output.
* When invoking this tool, the contents of the "command" parameter does NOT need to be XML-escaped.
* You do have access to a mirror of common linux and python packages via apt and pip.
* Each call starts a fresh shell. Use absolute paths or cd within the command.
* To inspect a particular line range of a file, e.g. lines 10-25, try 'sed -n 10,25p /path/to/the/file'.
* Please avoid commands that may produce a very large amount of output.
* Background processes are terminated when this call ends.""",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to run."
                }
            },
            "required": ["command"]
        }
    }

class BashSession:
    """A session of a bash shell."""
    def __init__(self):
        self._started = False
        self._process = None
        self._timed_out = False
        self._timeout = 120.0  # seconds
        self._sentinel = "__RSI_EXIT_" + uuid.uuid4().hex + "__"

    async def start(self):
        if self._started:
            return
        self._process = await asyncio.create_subprocess_shell(
            "/bin/bash --noprofile --norc",
            preexec_fn=os.setsid,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy()  # Ensures inheritance of the current environment
        )
        self._started = True

    def stop(self):
        if not self._started:
            return
        if self._process.returncode is None:
            self._process.terminate()
        self._process = None
        self._started = False

    async def run(self, command):
        if not self._started:
            raise ValueError("Session has not started.")
        if self._process.returncode is not None:
            raise ValueError(f"Bash has exited with returncode {self._process.returncode}")
        if self._timed_out:
            raise ValueError(
                f"Timed out: bash has not returned in {self._timeout} seconds and must be restarted."
            )
        
        # Send command
        self._process.stdin.write(
            command.encode() + (f"\nprintf '%03d%s\\n' \"$?\" '{self._sentinel}'\n"
                                f"printf '%s\\n' '{self._sentinel}' >&2\n").encode()
        )
        await self._process.stdin.drain()

        # Drain both pipes continuously; inspecting private buffers can deadlock
        # once asyncio pauses a full pipe before the completion marker arrives.
        total_chars = 0

        def count_output(size):
            nonlocal total_chars
            total_chars += size
            if total_chars > MAX_OUTPUT_CHARS:
                annotate(output_discarded=True, observed_chars_lower_bound=total_chars, output_limit_chars=MAX_OUTPUT_CHARS)
                raise ValueError(
                    f'Bash output exceeds the combined stdout/stderr limit of {MAX_OUTPUT_CHARS} characters '
                    f'(at least {total_chars} observed). Output discarded and command stopped. '
                    'Use head, sed, or a script to select fewer lines or fields before retrying.'
                )

        async def read_stream(stream):
            output = ''
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            marker = self._sentinel
            status_width = 3 if stream is self._process.stdout else 0
            counted = 0
            while chunk := await stream.read(4096):
                start = max(0, len(output) - len(marker) + 1)
                output += decoder.decode(chunk)
                index = output.find(marker, start)
                if index >= 0:
                    count_output(index - status_width - counted)
                    return output[:index], True
                # Hold back a possible split sentinel so it never counts as output.
                safe_end = max(counted, len(output) - len(marker) + 1 - status_width)
                count_output(safe_end - counted)
                counted = safe_end
            output += decoder.decode(b'', final=True)
            count_output(len(output) - counted)
            return output, False

        readers = [asyncio.create_task(read_stream(stream))
                   for stream in (self._process.stdout, self._process.stderr)]
        try:
            # Each pipe has its own marker: stdout readiness says nothing about
            # whether the final stderr bytes have been delivered to its reader.
            (output, stdout_marked), (stderr, stderr_marked) = await asyncio.wait_for(
                asyncio.gather(*readers),
                self._timeout)
            if stdout_marked:
                code = int(output[-3:])
                output = output[:-3]
            else:
                code = await asyncio.wait_for(self._process.wait(), self._timeout)
            annotate(exit_code=code, stdout_chars=len(output), stderr_chars=len(stderr), output_discarded=False)
            return output.strip(), stderr.strip(), code
        except asyncio.TimeoutError:
            self._timed_out = True
            annotate(timed_out=True)
            raise ValueError(f"Timed out: bash has not returned in {self._timeout} seconds and must be restarted.") from None
        except Exception as e:
            self._timed_out = True
            annotate(timed_out=True)
            raise ValueError(str(e))
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

def filter_error(error):
    # Filter out errors that we do not want to see
    filtered_lines = []
    i = 0
    error_lines = error.splitlines()
    while i < len(error_lines):
        line = error_lines[i]

        # Skip the next lines if ioctl error, add relevant lines
        if "Inappropriate ioctl for device" in line:
            i += 3
            if i < len(error_lines) and '<<exit>>' in error_lines[i]:
                i += 1
            while i < len(error_lines) - 1:
                filtered_lines.append(error_lines[i])
                i += 1
            i += 1
            continue

        filtered_lines.append(line)
        i += 1
    return '\n'.join(filtered_lines).strip()

async def tool_function_call(command):
    """Execute a command in the bash shell."""
    try:
        bash_session = BashSession()

        if not bash_session._started:
            await bash_session.start()

        output, error, code = await bash_session.run(command)
        error = filter_error(error)
        result = f"exit_code: {code}"
        if output:
            result += "\n" + output
        if error:
            result += "\nError:\n" + error
        return result.strip()
    except Exception as e:
        annotate(error_type=type(e).__name__)
        return f"Error: {str(e)}"
    finally:
        if bash_session._process is not None:
            process = bash_session._process
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.communicate(), CLEANUP_TIMEOUT)
            except asyncio.TimeoutError:
                pass  # A detached child may still hold the inherited pipes open.
            finally:
                process._transport.close()

def tool_function(command):
    return asyncio.run(tool_function_call(command))

if __name__ == "__main__":
    # Example usage
    import sys

    # Check if the script is called with arguments
    if len(sys.argv) < 2:
        print("Usage: python bash.py '<command>'")
    else:
        # Extract the command from the command-line arguments
        input_command = ' '.join(sys.argv[1:])
        # Run the tool_function asynchronously
        result = tool_function(input_command)
        print(result)

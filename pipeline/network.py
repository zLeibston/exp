"""API-only egress. Containers have no network; a Unix socket carries HTTP proxy traffic.

The host owns the destination allowlist. No credentials or response bodies are logged.
This file also supplies the small loopback relay inside each container.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import subprocess
import tempfile
import threading
from urllib.parse import urlsplit

POLICY = 'api-only-unix-proxy-v1'
GUEST = '/run/rsi-network'
PROXY = 'http://127.0.0.1:18765'
ENV = {key: PROXY for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                              'http_proxy', 'https_proxy', 'all_proxy')}
ENV.update(NO_PROXY='', no_proxy='')


def api_destination(config):
    # Match the adapters' default endpoints; explicit gateways always take precedence.
    defaults = {'anthropic': 'https://api.anthropic.com',
                'gemini': 'https://generativelanguage.googleapis.com'}
    url = config.url or defaults.get(config.protocol, 'https://api.deepseek.com'
              if config.model_name.startswith('deepseek') else 'https://api.openai.com')
    parts = urlsplit(url)
    return parts.hostname.lower(), parts.port or (443 if parts.scheme == 'https' else 80)


def pump(left, right):
    """Bounded-memory, full-duplex forwarding; EOF closes both ends."""
    while True:
        ready, _, _ = select.select([left, right], [], [], 900)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(128)
        self.stopping = False
        self.connections = set()
        self.lock = threading.Lock()
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    @contextmanager
    def track(self, connection):
        with self.lock:
            if self.stopping:
                connection.close()
                raise OSError("Proxy closed")
            self.connections.add(connection)
        try:
            yield connection
        finally:
            with self.lock:
                self.connections.discard(connection)
            connection.close()

    def close(self):
        self.shutdown()
        with self.lock:
            self.stopping = True
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        self.server_close()

    def handle_error(self, request, address):
        pass  # Disconnected clients are normal; never log request headers/credentials.


class Proxy(socketserver.BaseRequestHandler):
    def handle(self):
        with self.server.track(self.request):
            self.request.settimeout(30)
            reader = self.request.makefile('rb', buffering=0)
            try:
                line = reader.readline(8193)
                method, target, version = line.decode('ascii').strip().split(' ')
                headers, size = [], len(line)
                while True:
                    line = reader.readline(8193)
                    size += len(line)
                    if size > 65536 or not line or len(line) > 8192:
                        raise ValueError('Invalid proxy headers')
                    if line == b'\r\n':
                        break
                    headers.append(line)
                parts = urlsplit('//' + target if method == 'CONNECT' else target)
                port = parts.port or (443 if method == 'CONNECT' else 80)
                destination = ((parts.hostname or '').lower(), port)
                if (destination not in self.server.allowed or parts.username or parts.password
                        or (method != 'CONNECT' and parts.scheme != 'http')):
                    self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    return
                with self.server.track(socket.create_connection(destination, timeout=30)) as upstream:
                    upstream.settimeout(900)
                    self.request.settimeout(900)
                    if method == 'CONNECT':
                        self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    else:
                        # HTTP gateways: pin Host and close after one request. Do not forward
                        # proxy headers or permit a second destination through keep-alive.
                        path = parts.path or '/'
                        if parts.query:
                            path += '?' + parts.query
                        clean = [h for h in headers if h.split(b':', 1)[0].lower()
                                 not in {b'host', b'connection', b'proxy-connection', b'proxy-authorization'}]
                        upstream.sendall(f'{method} {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nConnection: close\r\n'.encode()
                                         + b''.join(clean) + b'\r\n')
                    pump(self.request, upstream)
            except (OSError, ValueError, UnicodeError):
                try:
                    self.request.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                except OSError:
                    pass
            finally:
                reader.close()


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as upstream:
            upstream.connect(GUEST + '/proxy.sock')
            self.request.settimeout(900)
            upstream.settimeout(900)
            pump(self.request, upstream)


class RelayServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def handle_error(self, request, address):
        pass


@contextmanager
def boundary(configs=()):
    """Yield Docker arguments and a command wrapper; no API profile means no egress."""
    base = ['--network', 'none', '--cap-drop', 'NET_RAW',
            '--security-opt', 'no-new-privileges=true']
    allowed = {api_destination(config) for config in configs}
    if not allowed:
        yield base, lambda command, python='python': command
        return
    with tempfile.TemporaryDirectory(prefix='rsi-network-') as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        server = Server(str(root / 'proxy.sock'), Proxy)
        server.allowed = allowed
        (root / 'proxy.sock').chmod(0o666)  # Container UID may differ; parent directory is read-only inside.
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            args = base + ['--mount', f'type=bind,source={root},target={GUEST},readonly',
                           '--mount', f'type=bind,source={Path(__file__).resolve()},target={GUEST}.py,readonly']
            for key, value in ENV.items():
                args += ['--env', f'{key}={value}']
            yield args, lambda command, python='python': [python, GUEST + '.py', *command]
        finally:
            server.close()
            thread.join()


def mounted_profiles(mounts):
    """The fixed worker credential mount is the authority, including role overrides."""
    from pipeline.api_config import load_config
    for source, target, _ in mounts:
        if target == '/run/secrets/api.yaml':
            data = json.loads(Path(source).read_text())
            roles = tuple(role for role in ('task_agent', 'meta_agent')
                          if role in data and data[role].get('model') != 'unused')
            return tuple(load_config(source, roles=roles).values())
    return ()


if __name__ == '__main__':
    import sys
    with RelayServer(('127.0.0.1', 18765), Relay) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        # docker exec inherits the Docker-configured proxy environment as well.
        os.environ.update(ENV)
        try:
            raise SystemExit(subprocess.call(sys.argv[1:]))
        finally:
            server.shutdown()

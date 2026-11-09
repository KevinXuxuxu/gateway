"""HTTP-level contract checks: what a client actually sees on the wire.

Everything here talks to a real gateway process over ONE reused TCP connection,
because that is how clients work: requests.Session, openai>=1.0 and curl all
keep the connection alive and send several requests down it. A test that opens a
fresh connection per call cannot see the bugs this file is about.

    uv run pytest -q

The fake engine answers 200 so requests reach the handler paths under test, and
records every body it received for the clamping assertions.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = "0" * 40
HDR = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
GOOD = json.dumps(
    {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}
).encode()
SEEN = []  # request bodies the fake engine received


class FakeEngine(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._reply(
            200, json.dumps({"object": "list", "data": [{"id": "fake-model"}]}).encode()
        )

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) or b"{}"
        try:
            SEEN.append(json.loads(raw))
        except Exception:
            SEEN.append(raw)
        self._reply(200, json.dumps({"id": "x", "choices": [], "usage": {}}).encode())

    def _reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True  # one response per connection, like a real 400


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_gateway(tmp_path, break_logs=False, **overrides):
    """Real gateway process + fake engine -> (proc, base_url, proxy_port).

    break_logs=True makes both log paths unwritable, which is what a read-only or
    full volume, or a failed mount, looks like to the code.
    """
    SEEN.clear()
    if break_logs:
        for name in ("access.jsonl", "usage.jsonl"):
            (tmp_path / name).mkdir()
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [{"name": "laptop", "key": KEY}]}))
    engine_port, proxy_port = free_port(), free_port()
    engine = ThreadingHTTPServer(("127.0.0.1", engine_port), FakeEngine)
    threading.Thread(target=engine.serve_forever, daemon=True).start()
    env = dict(
        os.environ,
        GATEWAY_USAGE_DIR=str(tmp_path),
        GATEWAY_KEYS_FILE=str(keys),
        GATEWAY_BACKEND_KEY="engine-key",
        GATEWAY_BACKENDS=f"fake=http://127.0.0.1:{engine_port}",
        GATEWAY_RATE_PER_MIN="120",
        GATEWAY_METRICS_PORT="0",
    )
    env.update(overrides)
    proc = subprocess.Popen(
        [
            sys.executable,
            os.path.join(ROOT, "api_proxy.py"),
            "--port",
            str(proxy_port),
            "--host",
            "127.0.0.1",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{proxy_port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if requests.get(f"{base}/health", timeout=1).status_code == 200:
                return proc, base, proxy_port
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("gateway did not come up")


def read_all(sock, wait=1.5):
    """Everything the server sends back on this connection, raw."""
    time.sleep(wait)
    sock.settimeout(3)
    out = b""
    try:
        while len(out) < 200000:
            chunk = sock.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        out += b"<TIMED OUT WAITING FOR EOF>"
    return out


def post_one(path, body, key=KEY, chunked=False):
    """One raw HTTP/1.1 request, byte-exact, so we control framing."""
    head = f"POST {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {key}\r\n"
    if chunked:
        head += "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
        return (head + f"{len(body):x}\r\n{body}\r\n0\r\n\r\n").encode()
    head += f"Content-Length: {len(body)}\r\n\r\n"
    return (head + body.decode()).encode()


# --------------------------------------------------------------------------- 1
def test_oversized_body_ends_the_connection(tmp_path):
    """A reply sent before the body was read must not leave that body behind.

    The gateway answers 413 without reading 4 KB of JSON. Those bytes used to be
    parsed as the *next* request line, so the second call on the same connection
    got 501 "Unsupported method" - and the client read a stale reply as its own.
    """
    proc, base, port = start_gateway(tmp_path, GATEWAY_MAX_BODY_BYTES="1024")
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    big = b'{"model":"fake-model","messages":[]}' + b"x" * 4096  # over the 1 KB limit
    sock.sendall(
        post_one("/v1/chat/completions", big) + post_one("/v1/chat/completions", GOOD)
    )
    data = read_all(sock)
    sock.close()
    proc.kill()
    assert data.startswith(b"HTTP/1.1 413"), data[:120]
    assert data.count(b"HTTP/1.1") == 1, (
        f"two replies on one connection: {data[:300]!r}"
    )
    # pre-fix: a 501 for the leftovers, or an html "Error response" 400
    assert b"501" not in data and b"Error response" not in data, data[:400]


def test_quota_429_on_a_reused_connection(tmp_path):
    """Five calls down one session with a quota of 2.

    Pre-fix the sequence was [200, 200, 429, 400, 429]: the 429 left the body
    unread, so call #4 was answered for bytes it never sent.
    """
    proc, base, _ = start_gateway(tmp_path, GATEWAY_RATE_PER_MIN="2")
    session = requests.Session()
    codes = [
        session.post(
            f"{base}/v1/chat/completions",
            data=GOOD,
            headers=HDR,
            timeout=20,
        ).status_code
        for _ in range(5)
    ]
    proc.kill()
    assert codes == [200, 200, 429, 429, 429], codes


def test_chunked_body_cannot_hijack_the_connection(tmp_path):
    """No Content-Length means the gateway reads nothing; same rule applies.

    Pre-fix a chunked caller got "unknown model None" plus a second reply built
    from its own body bytes, because nothing ended the connection.
    """
    proc, base, port = start_gateway(tmp_path)
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(
        post_one("/v1/chat/completions", GOOD, chunked=True)
        + post_one("/v1/chat/completions", GOOD)
    )
    data = read_all(sock)
    sock.close()
    proc.kill()
    assert data.count(b"HTTP/1.1") == 1, (
        f"two replies on one connection: {data[:300]!r}"
    )
    assert b"Error response" not in data and b"501" not in data, data[:400]


# --------------------------------------------------------------------------- 2
CRASH_SHAPES = [
    b"[1,2]",  # JSON array: no .get -> AttributeError
    b'"hello"',  # JSON string: same
    b'{"model":"fake-model","messages":"hello"}',  # iterating a str -> same
    b'{"model":"fake-model","max_tokens":"abc"}',  # int() -> ValueError
    b'{"model":"fake-model","max_tokens":"32.5"}',  # ...same
]


def test_unusual_bodies_get_an_answer_not_a_dropped_connection(tmp_path):
    """Five bodies that are valid JSON but not what the handler expected.

    Pre-fix each one raised inside do_POST: the thread died, the client saw
    RemoteDisconnected, and nothing was logged or counted.
    """
    proc, base, _ = start_gateway(tmp_path)
    for body in CRASH_SHAPES:
        r = requests.post(
            f"{base}/v1/chat/completions", data=body, headers=HDR, timeout=10
        )
        assert r.status_code in (200, 400), (body, r.status_code, r.text[:80])
        assert r.headers.get("Content-Type") == "application/json", (
            body,
            r.status_code,
        )
    proc.kill()


def test_bad_bodies_do_not_take_the_next_request_down(tmp_path):
    """The call after a malformed body must still work, on the same connection."""
    proc, base, _ = start_gateway(tmp_path)
    session = requests.Session()
    codes = []
    for body in CRASH_SHAPES + [GOOD]:
        codes.append(
            session.post(
                f"{base}/v1/chat/completions", data=body, headers=HDR, timeout=10
            ).status_code
        )
    proc.kill()
    assert codes[-1] == 200, codes  # the real request still gets a real answer


def test_max_tokens_clamp_only_touches_real_numbers(tmp_path):
    """The clamp must survive being made safe: ints still clamp, junk passes by."""
    proc, base, _ = start_gateway(tmp_path)
    requests.post(
        f"{base}/v1/chat/completions",
        data=b'{"model":"fake-model","max_tokens":999999,"max_output_tokens":500000}',
        headers=HDR,
        timeout=10,
    )
    requests.post(
        f"{base}/v1/chat/completions",
        data=b'{"model":"fake-model","max_tokens":"abc"}',
        headers=HDR,
        timeout=10,
    )
    proc.kill()
    assert SEEN[0]["max_tokens"] == 8192, SEEN[0]  # MAX_COMPLETION_TOKENS
    assert SEEN[0]["max_output_tokens"] == 8192, SEEN[0]
    assert SEEN[1]["max_tokens"] == "abc", SEEN[1]  # forwarded, not crashed on


# --------------------------------------------------------------------------- 3
def test_unwritable_logs_do_not_take_the_gateway_down(tmp_path):
    """Both log files unwritable, as they are on a read-only or full volume.

    Pre-fix the OSError from the log write propagated out of the handler, so
    /health and /readyz stopped answering too - which pulls the pod out of
    service. The same run also checks the request is still counted in metrics,
    which the failed write used to skip.
    """
    metrics_port = free_port()
    proc, base, _ = start_gateway(
        tmp_path,
        break_logs=True,
        GATEWAY_METRICS_PORT=str(metrics_port),
        GATEWAY_RATE_PER_MIN="2",
    )
    metrics = f"http://127.0.0.1:{metrics_port}/metrics"

    assert requests.get(f"{base}/health", timeout=5).status_code == 200
    assert requests.get(f"{base}/readyz", timeout=5).status_code == 200
    assert (
        requests.get(f"{base}/usage/summary", headers=HDR, timeout=5).status_code == 200
    )

    chat = f"{base}/v1/chat/completions"
    codes = [
        requests.post(chat, data=GOOD, headers=HDR, timeout=15).status_code
        for _ in range(4)
    ]
    denials = requests.get(f"{base}/nope", headers=HDR, timeout=5).status_code
    time.sleep(0.3)  # let the last writes be attempted
    text = requests.get(metrics, timeout=5).text
    proc.kill()
    proc.wait(timeout=10)
    out = proc.stdout.read()

    assert codes == [200, 200, 429, 429], codes  # quota still enforced, and answered
    assert denials == 404
    # the request was counted even though its log line went nowhere
    assert (
        'gateway_requests_total{key="laptop",path="/v1/chat/completions",status="200"} 2.0'
        in text
    )
    assert 'gateway_quota_denials_total{reason="rate_limit"} 2.0' in text
    # and the broken logging is said once per file, not once per request
    assert out.count("logging disabled") == 2, out[-500:]


def test_writable_logs_are_still_written(tmp_path):
    """The escape hatch must not quietly disable the file logs."""
    proc, base, _ = start_gateway(tmp_path)
    requests.post(f"{base}/v1/chat/completions", data=GOOD, headers=HDR, timeout=15)
    time.sleep(0.5)
    lines = (tmp_path / "access.jsonl").read_text().splitlines()
    usage = (tmp_path / "usage.jsonl").read_text().splitlines()
    proc.kill()
    assert len(lines) == 2, lines  # the readiness probe from the harness + the chat
    assert '"path": "/v1/chat/completions"' in lines[-1], lines[-1]
    assert len(usage) == 1, usage

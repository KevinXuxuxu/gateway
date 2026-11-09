"""End-to-end check of the /metrics endpoint against a fake MTPLX upstream.

    uv run pytest -q            (or: python -m pytest tests -q)

The fake upstream answers the three shapes the gateway treats differently:
non-streaming JSON, an SSE stream whose final chunk carries `usage`, and a 500.
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
USAGE_PATHS = (
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/messages",
    "/v1/responses",
)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeEngine(BaseHTTPRequestHandler):
    """Just enough of MTPLX's OpenAI-shaped API to exercise the gateway."""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            if self.headers.get("Authorization") != "Bearer engine-key":
                return self._send(401, b'{"error":"bad engine key"}')
            return self._send(
                200,
                json.dumps({"object": "list", "data": [{"id": "fake-model"}]}).encode(),
            )
        return self._send(404, b'{"error":"nope"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if body.get("model") == "fake-boom":
            return self._send(500, b'{"error":"engine exploded"}')
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 60},
        }
        if not body.get("stream"):
            return self._send(
                200,
                json.dumps(
                    {
                        "id": "x",
                        "choices": [
                            {"message": {"role": "assistant", "content": "ok"}}
                        ],
                        "usage": usage,
                    }
                ).encode(),
            )
        # SSE: token chunks, then a chunk carrying usage, then [DONE]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in [
            {"choices": [{"delta": {"content": "ok"}}]},
            {"choices": [{"delta": {}}], "usage": usage},
            "[DONE]",
        ]:
            payload = chunk if isinstance(chunk, str) else json.dumps(chunk)
            self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.flush()


def start_stack(tmp_path, rate_per_min="120"):
    """Fake engine + gateway as separate processes. Returns (proc, base, metrics)."""
    engine_port, proxy_port, metrics_port = free_port(), free_port(), free_port()
    engine = ThreadingHTTPServer(("127.0.0.1", engine_port), FakeEngine)
    threading.Thread(target=engine.serve_forever, daemon=True).start()

    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [{"name": "laptop", "key": KEY}]}))
    env = dict(
        os.environ,
        GATEWAY_USAGE_DIR=str(tmp_path),
        GATEWAY_KEYS_FILE=str(keys),
        GATEWAY_BACKEND_KEY="engine-key",
        GATEWAY_BACKENDS=f"fake=http://127.0.0.1:{engine_port}",
        GATEWAY_METRICS_PORT=str(metrics_port),
        GATEWAY_RATE_PER_MIN=rate_per_min,
        GATEWAY_LOG_STDOUT="1",
    )
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
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if requests.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise AssertionError("gateway did not come up:\n" + proc.stdout.read())
    return proc, base, f"http://127.0.0.1:{metrics_port}"


def metrics_text(proxy):
    return requests.get(f"{proxy[2]}/metrics", timeout=5).text


def wait_metrics(proxy, needles, timeout=10):
    """Poll /metrics until every needle shows up.

    The gateway records its counters just after writing the response, so a scrape
    issued straight away can be early. A single scrape made the suite fail on a
    few percent of runs with nothing wrong in the gateway.
    """
    deadline = time.time() + timeout
    while True:
        text = metrics_text(proxy)
        missing = [n for n in needles if n not in text]
        if not missing:
            return text
        if time.time() > deadline:
            raise AssertionError("never appeared: " + ", ".join(missing) + "\n" + text)
        time.sleep(0.05)


def sample(text, name):
    """All sample lines for a metric family (comments and HELP skipped)."""
    return [line for line in text.splitlines() if line.startswith(name + "{")]


def test_metrics_end_to_end(tmp_path):
    proc, base, metrics_base = start_stack(tmp_path)
    proxy = (proc, base, metrics_base)
    hdr = {"Authorization": f"Bearer {KEY}"}

    # the endpoint exists on its own port before any traffic
    assert "gateway_requests_total" in metrics_text(proxy)

    # probes must not be counted as traffic (they would swamp every rate query)
    for _ in range(5):
        requests.get(f"{base}/health", timeout=5)
    assert not sample(metrics_text(proxy), "gateway_requests_total")

    # ...and /metrics is never served on the app port, the one clients can reach
    leak = requests.get(f"{base}/metrics", headers=hdr, timeout=5)
    assert leak.status_code != 200 and "gateway_requests_total" not in leak.text

    payload = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}
    assert (
        requests.post(
            f"{base}/v1/chat/completions", json=payload, headers=hdr, timeout=30
        ).status_code
        == 200
    )
    # usage came from the engine: 100 prompt (60 cached) + 20 generated = 60 charged
    wait_metrics(
        proxy,
        [
            'gateway_requests_total{key="laptop",path="/v1/chat/completions",status="200"}',
            'gateway_tokens_total{backend="fake",key="laptop",kind="prompt"} 100.0',
            'gateway_tokens_total{backend="fake",key="laptop",kind="cached"} 60.0',
            'gateway_tokens_total{backend="fake",key="laptop",kind="charged"} 60.0',
        ],
    )

    # streaming: same counters, but tagged stream="true"
    assert (
        requests.post(
            f"{base}/v1/chat/completions",
            json=dict(payload, stream=True),
            headers=hdr,
            timeout=30,
        ).status_code
        == 200
    )
    wait_metrics(
        proxy,
        [
            'gateway_upstream_seconds_count{backend="fake",stream="true"} 1.0',
            'gateway_upstream_seconds_count{backend="fake",stream="false"} 1.0',
        ],
    )

    # auth failures are counted, with no key label invented from the presented token
    assert requests.get(f"{base}/v1/models", timeout=5).status_code == 401
    wait_metrics(
        proxy, ['gateway_requests_total{key="-",path="/v1/models",status="401"} 1.0']
    )

    # an engine error is counted too (that is the point of the 5xx panel)
    assert (
        requests.post(
            f"{base}/v1/chat/completions",
            json=dict(payload, model="fake-boom"),
            headers=hdr,
            timeout=30,
        ).status_code
        == 500
    )
    # in-flight is released: an empty gauge still reports what it saw
    wait_metrics(
        proxy,
        [
            'gateway_requests_total{key="laptop",path="/v1/chat/completions",status="500"} 1.0',
            'gateway_upstream_inflight{backend="fake"} 0.0',
        ],
    )
    proc.kill()


def test_quota_denial_is_counted(tmp_path):
    proc, base, metrics_base = start_stack(tmp_path, rate_per_min="2")
    proxy = (proc, base, metrics_base)
    hdr = {"Authorization": f"Bearer {KEY}"}
    payload = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}
    codes = [
        requests.post(
            f"{base}/v1/chat/completions", json=payload, headers=hdr, timeout=30
        ).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429], codes
    wait_metrics(
        proxy,
        [
            'gateway_quota_denials_total{reason="rate_limit"} 1.0',
            'gateway_requests_total{key="laptop",path="/v1/chat/completions",status="429"} 1.0',
        ],
    )
    proc.kill()

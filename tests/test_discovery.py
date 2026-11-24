"""Routing is discovery-based: model ids come from the engines, not from config.

    uv run pytest -q

Pins the swap story that replaced the GATEWAY_BACKENDS needle matching: a model
an engine starts serving mid-flight routes on the very next request, with no
gateway restart and no config change. Plus unit checks for the slot spec parser.
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
SERVED = {"ids": ["fake-model"]}  # what the fake engine currently advertises


class FakeEngine(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

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
            data = [{"id": mid} for mid in SERVED["ids"]]
            return self._send(
                200, json.dumps({"object": "list", "data": data}).encode()
            )
        return self._send(404, b'{"error":"nope"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        self._send(
            200,
            b'{"id":"x","choices":[{"message":{"role":"assistant","content":"ok"}}]}',
        )


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_gateway(tmp_path):
    """Fake engine + gateway as separate processes. Returns (proc, base)."""
    engine_port, proxy_port = free_port(), free_port()
    engine = ThreadingHTTPServer(("127.0.0.1", engine_port), FakeEngine)
    threading.Thread(target=engine.serve_forever, daemon=True).start()
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [{"name": "laptop", "key": KEY}]}))
    env = dict(
        os.environ,
        GATEWAY_USAGE_DIR=str(tmp_path),
        GATEWAY_KEYS_FILE=str(keys),
        GATEWAY_ENGINES=f"http://127.0.0.1:{engine_port}",
        GATEWAY_RATE_PER_MIN="120",
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
                return proc, base
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("gateway did not come up:\n" + proc.stdout.read())


def test_model_swap_is_picked_up_without_restart(tmp_path):
    proc, base = start_gateway(tmp_path)
    hdr = {"Authorization": f"Bearer {KEY}"}

    def post(model):
        return requests.post(
            f"{base}/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
            headers=hdr,
            timeout=10,
        )

    # what the engine serves routes by exact id
    assert post("fake-model").status_code == 200
    # something no engine serves is a clean 404, not a hang
    r = post("not-served")
    assert r.status_code == 404 and "unknown model" in r.json()["error"]["message"], (
        r.text
    )

    # the engine swaps models: the gateway notices on the request that misses
    SERVED["ids"] = ["fake-v2"]
    assert post("fake-v2").status_code == 200
    # the old id is gone from routing and from the advertised list
    assert post("fake-model").status_code == 404
    ids = [
        m["id"]
        for m in requests.get(f"{base}/v1/models", headers=hdr, timeout=10).json()[
            "data"
        ]
    ]
    assert ids == ["fake-v2"]
    proc.kill()


def test_parse_engines_slots():
    from api_proxy import parse_engines

    assert parse_engines("http://mac:8100-8102") == [
        "http://mac:8100",
        "http://mac:8101",
        "http://mac:8102",
    ]
    assert parse_engines("http://127.0.0.1:8100,http://127.0.0.1:8101") == [
        "http://127.0.0.1:8100",
        "http://127.0.0.1:8101",
    ]
    assert parse_engines("http://mac") == ["http://mac"]
    assert parse_engines(" http://mac:8100-8101 , http://mac2:80 ") == [
        "http://mac:8100",
        "http://mac:8101",
        "http://mac2:80",
    ]
    for bad in ("http://mac:8110-8100", "mac:8100", "http://mac:nope", ""):
        try:
            parse_engines(bad)
            raise AssertionError(f"accepted {bad!r}")
        except SystemExit:
            pass

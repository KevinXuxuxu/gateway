#!/usr/bin/env python
"""Manage API keys for the local model pair (shared by both models).

    python scripts/keys.py add <name>      # mint a new key
    python scripts/keys.py list            # show keys (name, prefix, created)
    python scripts/keys.py revoke <name>   # delete a key
"""
import json
import os
import secrets
import sys
import time

HOME = os.path.expanduser("~")
def _env(name, default=""):
    """GATEWAY_* env vars, with one-release fallback to the legacy BENCH_* names."""
    v = os.environ.get("GATEWAY_" + name)
    return os.environ.get("BENCH_" + name, default) if v is None else v


STATE_DIR = _env("USAGE_DIR", os.path.join(HOME, ".llm-gateway"))
KEYS = _env("KEYS_FILE", os.path.join(STATE_DIR, "keys.json"))


def load():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(KEYS):
        return {"keys": []}
    return json.load(open(KEYS))


def save(d):
    json.dump(d, open(KEYS, "w"), indent=2)
    os.chmod(KEYS, 0o600)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    d = load()
    if cmd == "add":
        name = sys.argv[2] if len(sys.argv) > 2 else f"key{len(d['keys'])+1}"
        key = secrets.token_hex(20)
        d["keys"].append({"name": name, "key": key,
                          "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        save(d)
        print(f"added '{name}': {key}")
        print("(both models accept it; the proxy reloads the file on every request)")
    elif cmd == "revoke":
        name = sys.argv[2]
        before = len(d["keys"])
        d["keys"] = [k for k in d["keys"] if k.get("name") != name]
        save(d)
        print(f"revoked {before - len(d['keys'])} key(s) named '{name}'")
    else:
        if not d["keys"]:
            print("no keys yet: python scripts/keys.py add <name>")
        for k in d["keys"]:
            print(f"  {k['name']:16s} {k['key'][:14]}…  created {k.get('created','?')}")


if __name__ == "__main__":
    main()

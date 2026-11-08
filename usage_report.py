#!/usr/bin/env python
"""Summarise local model usage.

Sources:
  ~/.bench-usage/usage.jsonl          per-request records written by api_proxy.py
                                      (key, model, tokens, latency)
  ~/.mtplx/logs/request-log-<port>.jsonl
                                      MTPLX's own 350-field per-request receipts
                                      (cache, TTFT, decode tok/s, thermal, ...)

    python scripts/usage_report.py            # by key x model, plus per-model totals
    python scripts/usage_report.py --days 1   # only the last N days
    python scripts/usage_report.py --json
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import sys

HOME = os.path.expanduser("~")
USAGE = os.environ.get("BENCH_USAGE_LOG", os.path.join(HOME, ".bench-usage", "usage.jsonl"))
MTPLX_LOGS = os.path.join(HOME, ".mtplx", "logs", "request-log-*.jsonl")


def parse_ts(s):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def load_proxy(days=None):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days) if days else None
    rows = []
    try:
        for line in open(USAGE):
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = parse_ts(r.get("ts", ""))
            if cutoff and ts and ts < cutoff:
                continue
            rows.append(r)
    except FileNotFoundError:
        pass
    return rows


def load_mtplx(days=None):
    cutoff = (dt.datetime.now().timestamp() - days * 86400) if days else None
    rows = []
    for path in glob.glob(MTPLX_LOGS):
        port = path.rsplit("-", 1)[-1].split(".")[0]
        try:
            for line in open(path):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ts = r.get("logged_at_s")
                if cutoff and ts and ts < cutoff:
                    continue
                r["_port"] = port
                rows.append(r)
        except FileNotFoundError:
            continue
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    proxy = load_proxy(args.days)
    mtplx = load_mtplx(args.days)

    by_key = collections.defaultdict(lambda: {"requests": 0, "prompt": 0, "cached": 0,
                                              "charged": 0, "completion": 0, "seconds": 0.0})
    for r in proxy:
        a = by_key[(r.get("key", "?"), r.get("model", "?"))]
        a["requests"] += 1
        a["prompt"] += r.get("prompt_tokens") or 0
        a["cached"] += r.get("cached_tokens") or 0
        a["charged"] += r.get("charged_tokens") or 0
        a["completion"] += r.get("completion_tokens") or 0
        a["seconds"] += r.get("elapsed_s") or 0.0

    by_model = collections.defaultdict(lambda: {"requests": 0, "prompt": 0, "cached": 0,
                                                "charged": 0, "completion": 0,
                                                "ttft": [], "decode": []})
    for r in mtplx:
        a = by_model[r.get("served_model_id") or f"port{r['_port']}"]
        a["requests"] += 1
        a["prompt"] += r.get("prompt_tokens") or 0
        a["cached"] += r.get("cached_tokens") or 0
        a["charged"] += r.get("charged_tokens") or 0
        a["completion"] += r.get("completion_tokens") or 0
        if r.get("ttft_s") is not None:
            a["ttft"].append(r["ttft_s"])
        if r.get("decode_tok_s"):
            a["decode"].append(r["decode_tok_s"])

    out = {"proxy": [{"key": k[0], "model": k[1], **v} for k, v in sorted(by_key.items())],
           "mtplx": [{"model": m, "requests": v["requests"], "prompt_tokens": v["prompt"],
                      "cached_tokens": v["cached"], "completion_tokens": v["completion"],
                      "ttft_p50_s": round(sorted(v["ttft"])[len(v["ttft"]) // 2], 3) if v["ttft"] else None,
                      "decode_tok_s_avg": round(sum(v["decode"]) / len(v["decode"]), 1) if v["decode"] else None}
                     for m, v in sorted(by_model.items())]}
    if args.json:
        print(json.dumps(out, indent=2)); return 0

    print("== usage by API key (from the proxy log)")
    if not out["proxy"]:
        print("   no proxy records yet")
    for r in out["proxy"]:
        print(f"   {r['key']:10s} {r['model'][:40]:40s} req={r['requests']:5d} "
              f"in={r['prompt']:9d} (cached {r['cached']:9d}) out={r['completion']:7d} "
              f"compute={r['charged']:9d} {r['seconds']:7.1f}s")
    print("\n== per-model traffic (from MTPLX request logs)")
    for r in out["mtplx"]:
        print(f"   {r['model'][:44]:44s} req={r['requests']:6d} in={r['prompt_tokens']:9d} "
              f"out={r['completion_tokens']:9d} ttft~{r['ttft_p50_s']}s decode~{r['decode_tok_s_avg']} tok/s")
    if out["mtplx"]:
        tot_in = sum(r["prompt_tokens"] for r in out["mtplx"])
        tot_out = sum(r["completion_tokens"] for r in out["mtplx"])
        print(f"\n   TOTAL in={tot_in:,} out={tot_out:,} tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())

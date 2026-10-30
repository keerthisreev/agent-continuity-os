"""One-request Nebius Token Factory smoke test. No layer, no demo, no retries.

Usage:
    NEBIUS_API_KEY=<key> CONTINUITY_LIVE=1 python3 smoke_nebius.py
    # or: NEBIUS_API_KEY=<key> python3 smoke_nebius.py --live

Captures exactly one request: HTTP status, model returned, elapsed time,
token usage, and the response text. Any error is printed with the key
redacted. This is the first live check; the full demo comes later.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import requests

from continuity.nemotron import live_enabled

BASE_URL = "https://api.tokenfactory.nebius.com/v1"
MODEL = "nvidia/Nemotron-3.5-Super-VL"


def main() -> int:
    ap = argparse.ArgumentParser(description="One-request Nebius smoke test")
    ap.add_argument("--live", action="store_true",
                    help="explicitly authorize this single live request")
    args = ap.parse_args()
    api_key = os.environ.get("NEBIUS_API_KEY", "")
    if not api_key:
        print("smoke: NEBIUS_API_KEY is not set -- refusing to run", file=sys.stderr)
        return 2
    if not (args.live or live_enabled()):
        # Explicit opt-in, like the rest of the codebase: the documented
        # rule is CONTINUITY_LIVE=1 or --live. Keep the rule uniform.
        print("smoke: refusing -- set CONTINUITY_LIVE=1 or pass --live to "
              "authorize this single live request", file=sys.stderr)
        return 2
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": 'Reply with exactly {"ok": true}.'}],
        "temperature": 0,
        "max_tokens": 256,
    }
    started = time.perf_counter()
    try:
        resp = requests.post(
            BASE_URL + "/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        elapsed = time.perf_counter() - started
        print(f"HTTP status: {resp.status_code}")
        print(f"elapsed: {elapsed:.2f}s")
        resp.raise_for_status()
        data = resp.json()
        print(f"model: {data.get('model')}")
        print(f"usage: {data.get('usage')}")
        print(f"response: {data['choices'][0]['message']['content']!r}")
    except Exception as exc:  # noqa: BLE001 - report, don't propagate
        elapsed = time.perf_counter() - started
        safe = str(exc).replace(api_key, "[REDACTED]")
        print(f"ERROR after {elapsed:.2f}s: {safe}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

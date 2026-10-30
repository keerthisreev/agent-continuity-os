"""Reproducible benchmark: Baselines A/B/C + honest Baseline D status.

  A: unprotected workflow
  B: idempotency-only protection (lost_ack only; keys cannot stop an
     unsafe action or detect a silent non-action, so B is n/a elsewhere)
  C: deterministic continuity layer
  D: deterministic layer + Nemotron-assisted adversarial testing
     (live evidence from docs/evidence/live-canary-remediation.txt when
     present; otherwise reported as NOT YET MEASURED, never faked)

Every A/B/C cell is computed by running sim/ablation.run_challenge
in-process — nothing is hard-coded. D is reported from the live
evidence file only.

Run:  python3 sim/benchmark.py [--md]   (prints a table; --md = markdown)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.ablation import run_challenge


def _evidence_d() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "docs", "evidence",
        "live-canary-remediation.txt")
    if not os.path.exists(path):
        return {"status": "NOT YET MEASURED",
                "note": "live canary evidence file absent; run "
                        "test_live_nebius.py with NEBIUS_API_KEY + "
                        "CONTINUITY_LIVE=1 first"}
    with open(path) as fh:
        text = fh.read()
    return {"status": "MEASURED (live)",
            "note": text.strip().replace("\n", " | ")}


def run_all() -> dict:
    return {
        "lost_ack": run_challenge("lost_ack"),
        "unsafe": run_challenge("unsafe"),
        "stale_write": run_challenge("stale_write"),
        "baseline_d": _evidence_d(),
    }


def _three_way_rows(comparison: dict) -> dict:
    rows = {r["aspect"]: r for r in comparison.get("rows", [])}
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args(argv)
    r = run_all()

    la = _three_way_rows(r["lost_ack"]["comparison"])
    bookings = la.get("Ride bookings created (one pickup requested)", {})
    amnesia = la.get("Provider loses its state before the retry (measured)", {})
    unsafe_rows = _three_way_rows(r["unsafe"]["comparison"])
    unsafe_action = unsafe_rows.get("Invariant-violating action", {})
    unsafe_detect = unsafe_rows.get("Semantic failure detected", {})
    stale_rows = _three_way_rows(r["stale_write"]["comparison"])
    stale_write = stale_rows.get("Delayed write from the fenced primary", {})
    stale_effects = stale_rows.get("Effects for one intended action", {})

    if args.md:
        print("| scenario | A unprotected | B idempotency-only | C layer | D Nemotron-assisted |")
        print("|---|---|---|---|---|")
        print(f"| lost_ack (1 pickup) | {bookings.get('baseline', '?')} | "
              f"{bookings.get('idempotency_only', '?')} | "
              f"{bookings.get('protected', '?')} | "
              f"{r['baseline_d']['status']} |")
        print(f"| lost_ack + provider amnesia | {amnesia.get('baseline', '?')} | "
              f"{amnesia.get('idempotency_only', '?')} | "
              f"{amnesia.get('protected', '?')} | |")
        print(f"| unsafe action (db restart) | "
              f"{unsafe_action.get('baseline', '?')} | n/a | "
              f"{unsafe_action.get('protected', '?')} | |")
        print(f"| unsafe detection | {unsafe_detect.get('baseline', '?')} | "
              f"n/a | {unsafe_detect.get('protected', '?')} | |")
        print(f"| stale write | {stale_write.get('baseline', '?')} | n/a | "
              f"{stale_write.get('protected', '?')} | |")
        print(f"| stale effects for 1 action | "
              f"{stale_effects.get('baseline', '?')} | n/a | "
              f"{stale_effects.get('protected', '?')} | |")
    else:
        print("BENCHMARK (computed in-process via sim/ablation.run_challenge)")
        print("=" * 70)
        print(f"lost_ack bookings: A={bookings.get('baseline')} | "
              f"B={bookings.get('idempotency_only')} | "
              f"C={bookings.get('protected')}")
        print(f"lost_ack + amnesia: A={amnesia.get('baseline')} | "
              f"B={amnesia.get('idempotency_only')} | "
              f"C={amnesia.get('protected')}")
        print(f"unsafe action: A={unsafe_action.get('baseline')} | "
              f"C={unsafe_action.get('protected')}")
        print(f"unsafe detection: A={unsafe_detect.get('baseline')} | "
              f"C={unsafe_detect.get('protected')}")
        print(f"stale_write: A={stale_write.get('baseline')} | "
              f"C={stale_write.get('protected')}")
        print(f"baseline D: {r['baseline_d']['status']} — "
              f"{r['baseline_d']['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

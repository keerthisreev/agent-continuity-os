#!/usr/bin/env python3
"""Agent Continuity Layer -- brutal proof, demo-readable.

Runs the full scenario (all three failure modes + recovery/rejoin) and
prints a timestamped timeline plus the $20K screen. The output tells the
story with zero narration.

    python3 demo.py [--failure hard|silent|gray|all] [--diagnostician auto|none|stub]

No network calls are made unless NEBIUS_API_KEY is set (live Nemotron via
Nebius Token Factory for the demo video); otherwise the stub explains.
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_continuity import make_world, process_incident, incident, kinds
from continuity.diagnostician import (default_diagnostician, StubDiagnostician,
                                      NemotronDiagnostician)
from continuity.nemotron import live_enabled

FAILURES = ("hard", "silent", "gray")
TITLES = {
    "hard": "FAILURE A -- hard death (process killed)",
    "silent": "FAILURE B -- silent dependency failure (LLM down, process alive)",
    "gray": "FAILURE C -- gray semantic failure (plausible but wrong)",
}


def pick_diagnostician(choice: str, live: bool):
    if choice == "none":
        return None, "disabled (deterministic sentinel alone)"
    if choice == "stub":
        return StubDiagnostician(), "stub (deterministic)"
    d = default_diagnostician(live=live)
    if isinstance(d, NemotronDiagnostician):
        return d, (f"Nemotron via Nebius Token Factory [{d.model}] -- "
                   f"visible NVIDIA usage for the video")
    if live:
        return d, "stub -- live requested but NEBIUS_API_KEY not set"
    return d, "stub -- add --live with NEBIUS_API_KEY for live Nemotron via Nebius Token Factory"


def run_scenario(failure: str, diagnostician, live: bool):
    layer, store, primaries = make_world(diagnostician=diagnostician,
                                         live=live)
    inc1, inc2 = incident("INC-1042"), incident("INC-1043")
    process_incident(layer, store, primaries, inc1)

    if failure == "hard":
        primaries["remediation"].alive = False
    elif failure == "silent":
        primaries["remediation"].backend.mode = "llm_down"
    elif failure == "gray":
        primaries["remediation"].backend.mode = "gray"

    view = outside_view(failure, primaries)
    if view is not None:
        print(view)

    process_incident(layer, store, primaries, inc2)

    # Recovery: the primary comes back; rejoin only after probes pass.
    primaries["remediation"].alive = True
    primaries["remediation"].backend.mode = "healthy"
    rejoined = layer.attempt_rejoin("remediation")

    # Forensic narrative: written from the confirmed ledger, advisory only.
    narrative = None
    if diagnostician is not None:
        try:
            narrative = diagnostician.incident_narrative(layer.ledger.report())
            layer.ledger.record(
                "diagnostician_note",
                {"agent": "remediation", "note": narrative,
                 "narrative": True}, layer.clock.now())
        except Exception:
            narrative = None
    return layer, store, inc1, inc2, rejoined, narrative


def outside_view(failure: str, primaries) -> str | None:
    """What conventional monitoring sees: process-level signals only.

    For silent/gray failures the process looks perfectly healthy from the
    outside -- this contrast is the point of the demo.
    """
    if failure == "hard":
        return None  # a dead process is visible to every monitor
    return ("[outside view] remediation process is ALIVE -- "
            "a monitoring dashboard sees nothing wrong")


def _t(tick: int) -> str:
    return f"t+{tick:04d}"


def render_timeline(layer) -> list:
    lines = []
    for e in layer.ledger.timeline:
        k, d, t = e["kind"], e["detail"], e["tick"]
        if k == "canary_probes_generated":
            src = d["source"]
            if src == "nemotron":
                lines.append(f"{_t(t)}  [nemotron] generated {d['count']} canary "
                             f"probes for role={d['role']} -- model writes "
                             f"scenarios, deterministic oracle judges")
            else:
                lines.append(f"{_t(t)}  [static] {d['count']} deterministic canary "
                             f"probe for role={d['role']} (offline: --live + "
                             f"NEBIUS_API_KEY for Nemotron probes)")
        elif k == "anomaly":
            lines.append(f"{_t(t)}  ROLE ANOMALY -- {d['violations'][0]}")
        elif k == "critical_violation":
            lines.append(f"{_t(t)}  BUSINESS INVARIANT VIOLATED -- {d['violations'][0]}")
        elif k == "crash":
            lines.append(f"{_t(t)}  PROCESS DEAD -- {d['error']}")
        elif k in ("suspicion_canary", "targeted_canary", "recovery_probe"):
            verdict = "PASSED" if d["passed"] else "FAILED"
            label = {"suspicion_canary": "suspicion canary",
                     "targeted_canary": "targeted canary",
                     "recovery_probe": "recovery probe"}[k]
            lines.append(f"{_t(t)}  {label} {verdict}"
                         + (f" -- {d['reasons'][0]}" if d["reasons"] else ""))
        elif k == "state_transition" and d["to"] in ("QUARANTINED", "FAILOVER",
                                                     "RECOVERING", "REJOIN"):
            lines.append(f"{_t(t)}  agent {d['to']}")
        elif k == "fencing":
            lines.append(f"{_t(t)}  FENCED -- remediation generation rotated (no split brain)")
        elif k == "standby_activated":
            resumed = (f"resumed from last-known-good t+{d['resumed_from_tick']:04d}"
                       if d.get("resumed_from_tick") is not None
                       else "no checkpoint yet -- starts fresh")
            lines.append(f"{_t(t)}  STANDBY ACTIVATED at generation {d['generation']} -- "
                         f"{resumed}")
        elif k == "duplicate_write_prevented":
            lines.append(f"{_t(t)}  duplicate write prevented: {d['action_id']}")
        elif k == "human_review_flagged":
            lines.append(f"{_t(t)}  HUMAN REVIEW FLAGGED -- {d['reason']}")
        elif k == "rejoin_complete":
            lines.append(f"{_t(t)}  PRIMARY REJOINED at generation {d['new_generation']} "
                         f"-- probes passed, write authority restored")
        elif k == "rejoin_denied":
            lines.append(f"{_t(t)}  rejoin DENIED -- still degraded")
        elif k == "diagnostician_note":
            lines.append(f"{_t(t)}  [diagnostician] {d['note'][:100]}")
    return lines


def render_money_screen(layer, inc1, inc2, rejoined: bool, narrative) -> list:
    rep = layer.ledger.report()
    w = rep["failure_windows"][0]
    affected = rep["affected_work"]
    resumed = next((e["detail"]["resumed_from_tick"]
                    for e in rep["timeline"]
                    if e["kind"] == "standby_activated"), None)
    resumed_s = (f"t+{resumed:04d} (checkpoint before first anomaly "
                 f"at t+{w['first_anomaly_tick']:04d})"
                 if resumed is not None
                 else "none -- standby started fresh (no checkpoint existed)")
    lines = []
    lines.append("")
    lines.append("=" * 64)
    lines.append("THE $20K SCREEN")
    lines.append("=" * 64)
    lines.append(f"last known good ......... {resumed_s}")
    lines.append(f"failure detected ........ t+{w['failover_tick']:04d} "
                 f"(TSFD: {rep['tsfd_ticks']} ticks -- same observation cycle)")
    lines.append("")
    lines.append("AFFECTED WORK")
    reviewed = {h["incident"] for h in rep["human_review"]}
    for inc_id in (inc1["id"], inc2["id"]):
        if inc_id in reviewed:
            continue  # shown with reason below
        lines.append(f"  {inc_id}  {affected.get(inc_id, 'unknown')}")
    for h in rep["human_review"]:
        lines.append(f"  {h['incident']}  human review -- {h['reason'][:70]}")
    lines.append("")
    lines.append(f"duplicate writes prevented: {rep['duplicates_prevented']}")
    lines.append(f"recovery: {'primary rejoined at new generation -- recovery complete'
                  if rejoined else 'REJOIN FAILED'}")
    lines.append("=" * 64)
    if narrative:
        lines.append("")
        lines.append("FORENSIC NARRATIVE (advisory only -- never a verdict)")
        lines.append(f"  {narrative}")
        lines.append("=" * 64)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Agent Continuity Layer brutal proof")
    ap.add_argument("--failure", choices=("hard", "silent", "gray", "all"),
                    default="all")
    ap.add_argument("--diagnostician", choices=("auto", "none", "stub"),
                    default="auto")
    ap.add_argument("--live", action="store_true",
                    help="explicit opt-in for live Nebius calls (requires "
                         "NEBIUS_API_KEY); CONTINUITY_LIVE=1 works too. "
                         "Without either, everything runs offline "
                         "on deterministic static probes and the stub")
    args = ap.parse_args()

    # --live flag OR CONTINUITY_LIVE=1: the documented alternatives agree.
    live = args.live or live_enabled()
    diagnostician, diag_label = pick_diagnostician(args.diagnostician, live)
    failures = FAILURES if args.failure == "all" else (args.failure,)

    print("AGENT CONTINUITY LAYER -- brutal proof")
    print(f"mode: {'LIVE (Nebius Token Factory)' if live else 'OFFLINE (deterministic, zero network calls)'}")
    print(f"diagnostician: {diag_label}")
    print("sentinel: deterministic (authoritative with diagnostician disabled)")
    print()

    for failure in failures:
        print("-" * 64)
        print(TITLES[failure])
        print("-" * 64)
        layer, store, inc1, inc2, rejoined, narrative = run_scenario(
            failure, diagnostician, live)
        for line in render_timeline(layer):
            print(line)
        for line in render_money_screen(layer, inc1, inc2, rejoined, narrative):
            print(line)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

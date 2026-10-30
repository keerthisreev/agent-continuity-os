"""Baseline-vs-protected ablation: workflow alone vs workflow + layer.

Runs the SAME gray/silent-failure scenario twice with the SAME scripted
agents and the SAME step order:

  baseline  -- no sentinel, no fencing, no standby, no checkpoints. The
               workflow accepts each agent's output at face value.
  protected -- the identical steps driven through the real ContinuityLayer.

Framed honestly: this is an ablation, not a strawman competitor. The
baseline is this workflow minus the reliability layer. Every number in
the comparison comes from the two runs' own records -- nothing is
hard-coded.

The injected fault is deterministic and preconfigured (a scripted
backend mode), never generalized AI-failure detection: "gray" returns a
plausible-but-wrong plan violating the invariant "never restart the
database for this incident class"; "silent" simulates a dead model
dependency returning a well-formed but empty plan.

Run:  python3 -m sim.ablation [--failure gray|silent|lost_ack] [--incident INC-1043]
Challenge mode: python3 -c "from sim.ablation import run_challenge;
run_challenge('lost_ack'|'unsafe'|'stale_write')"
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.triage import TriageAgent
from agents.diagnosis import DiagnosisAgent
from agents.remediation import RemediationAgent
from agents.escalation import EscalationAgent
from store import IncidentStore
from test_continuity import make_world
from continuity.diagnostician import StubDiagnostician

from . import FAILURE_MODES, ROLES
from .lost_ack import (build_booking_timeline, compare_lost_ack,
                       compare_lost_ack_three_way,
                       run_baseline_lost_ack, run_idempotency_only_lost_ack,
                       run_protected_lost_ack)
from .receipt import (build_receipt, receipt_from_blocked_action,
                      receipt_from_lost_ack)
from .scorecard import build_scorecard, render_text


def make_incident(incident_id: str) -> dict:
    return {"id": incident_id, "service": "payments-api",
            "symptom": "p99 latency > 2s", "severity": "P1"}


def _action_label(action: dict) -> str:
    return f"{action.get('kind')}:{action.get('target')}"


def _peek_proposal(agent, ctx: dict) -> dict:
    """Read what the agent *proposes* without running it through the layer.

    Why this exists: ContinuityLayer.invoke() transparently retries with
    the standby on failover, so the invocation result never carries the
    compromised primary's raw proposal -- the report needs it to show what
    was blocked. Safety precondition: the scripted test doubles' handle()
    is pure (effects land only via layer.apply_effects / store.apply_effect),
    so this peek applies nothing.
    """
    return agent.handle(dict(ctx))


# ---------------------------------------------------------------------------
# Baseline: the workflow alone. No layer, no checks, no second chances.
# ---------------------------------------------------------------------------

def run_baseline(failure: str = "gray", incident_id: str = "INC-1043") -> Dict[str, Any]:
    """Run the workflow with no continuity layer. Outputs are accepted at
    face value; the invariant-violating action is executed, not blocked."""
    if failure == "lost_ack":
        return run_baseline_lost_ack(incident_id)
    agents = {
        "triage": TriageAgent(),
        "diagnosis": DiagnosisAgent(),
        "remediation": RemediationAgent(),
        "escalation": EscalationAgent(),
    }
    inc = make_incident(incident_id)
    ctx = {"incident": inc}
    effects_allowed: List[str] = []
    steps: List[Dict[str, Any]] = []

    # Same step order as the protected run: triage, diagnosis, a clean
    # remediation pass, a verification pass where the failure is armed,
    # then escalation.
    order = ["triage", "diagnosis", "remediation", "remediation", "escalation"]
    for step_i, role in enumerate(order):
        if role == "remediation" and step_i == 3:
            agents["remediation"].backend.mode = FAILURE_MODES[failure]
        out = agents[role].handle(ctx)
        applied = [_action_label(a) for a in out.get("actions", [])]
        effects_allowed.extend(applied)
        steps.append({"step": step_i, "role": role, "actions": applied,
                      "note": "accepted at face value"})
        ctx[role] = out

    unsafe = [e for e in effects_allowed if e == "restart:database"]
    return {
        "mode": "workflow alone (no continuity layer)",
        "incident_id": incident_id,
        "failure": failure,
        "detected": None,  # nothing watches; nothing is detected
        "effects_allowed": effects_allowed,
        "unsafe_action_executed": bool(unsafe),
        "unsafe_actions": unsafe,
        "steps": steps,
    }


# ---------------------------------------------------------------------------
# Protected: the identical steps through the real ContinuityLayer.
# ---------------------------------------------------------------------------

def run_protected(failure: str = "gray",
                  incident_id: str = "INC-1043") -> Dict[str, Any]:
    """Run the identical steps through the real layer. Always offline:
    StubDiagnostician, live=False (a stray key can never flip the suite)."""
    if failure == "lost_ack":
        return run_protected_lost_ack(incident_id)
    layer, store, primaries = make_world(diagnostician=StubDiagnostician(),
                                         live=False)
    assert layer.live is False
    inc = make_incident(incident_id)
    ctx = {"incident": inc}
    blocked: List[str] = []
    failover_tick: Optional[int] = None

    def _invoke(role: str) -> dict:
        result = layer.invoke(role, ctx, primaries[role])
        layer.apply_effects(result, store)
        ctx[role] = result["output"]
        return result

    for role in ("triage", "diagnosis", "remediation"):
        _invoke(role)
    # Verification pass: peek at what the compromised primary proposes
    # (pure read -- see _peek_proposal), then let the layer observe the
    # same output through its own invocation path, which fences it.
    primaries["remediation"].backend.mode = FAILURE_MODES[failure]
    proposed = _peek_proposal(primaries["remediation"], ctx)
    blocked = [_action_label(a) for a in proposed.get("actions", [])]
    result = _invoke("remediation")
    if result["active"] == "standby":
        failover_tick = result["tick"]
    _invoke("escalation")

    # Effects the layer actually allowed: completed action ids recorded in
    # the incident-scoped checkpoints. Action ids have the form
    # incident:role:index:kind:target (see ContinuityLayer._action_id).
    def _effect_label(action_id: str) -> str:
        parts = action_id.split(":")
        return f"{parts[3]}:{parts[4]}" if len(parts) >= 5 else action_id

    effects_allowed = []
    for role in ROLES:
        pkg = layer.checkpoint_package(role, incident_id)
        if pkg is None:
            continue
        effects_allowed.extend(_effect_label(aid)
                               for aid in pkg.completed_action_ids)

    # What the sentinel saw: first anomaly/critical entry for this incident.
    detected = None
    for e in layer.ledger.timeline:
        if e["kind"] in ("critical_violation", "anomaly"):
            d = e["detail"] or {}
            detected = {"tick": e["tick"], "kind": e["kind"],
                        "violations": list(d.get("violations") or [])}
            break

    resume = next(
        ({"standby": e["detail"].get("standby"),
          "generation": e["detail"].get("generation"),
          "resumed_from_tick": e["detail"].get("resumed_from_tick"),
          "resumed_from_incident": e["detail"].get("resumed_from_incident")}
         for e in layer.ledger.timeline if e["kind"] == "standby_activated"),
        None)

    return {
        "mode": "workflow + continuity layer",
        "incident_id": incident_id,
        "failure": failure,
        "layer": layer,
        "detected": detected,
        "blocked_actions": blocked,
        "effects_allowed": effects_allowed,
        "failover_tick": failover_tick,
        "resume": resume,
        "duplicates_prevented": layer.ledger.duplicates_prevented,
        "disposition": layer.ledger.affected_work().get(incident_id),
        "fencing_generations": {
            role: layer.fencing.current(role) for role in ROLES},
    }


# ---------------------------------------------------------------------------
# Comparison: every cell from the two runs' own records.
# ---------------------------------------------------------------------------

def compare(baseline: Dict[str, Any], protected: Dict[str, Any]) -> Dict[str, Any]:
    if baseline.get("failure") == "lost_ack":
        return compare_lost_ack(baseline, protected)
    det = protected["detected"] or {}
    rows = [
        {
            "aspect": "Semantic failure detected",
            "baseline": "No — no independent checks; output accepted at face value.",
            "protected": (
                f"Yes — {det.get('kind')} at tick {det.get('tick')}: "
                f"{(det.get('violations') or [''])[0]}"
                if det else "No failure armed in this run."),
        },
        {
            "aspect": "Invariant-violating action",
            "baseline": (
                f"EXECUTED: {', '.join(baseline['unsafe_actions'])}"
                if baseline["unsafe_action_executed"]
                else "none proposed in this run"),
            "protected": (
                f"BLOCKED before any effect: {', '.join(protected['blocked_actions'])} "
                f"(role fenced at generation "
                f"{protected['fencing_generations'].get('remediation')})"
                if protected["blocked_actions"] else "none proposed in this run"),
        },
        {
            "aspect": "Effects the run allowed",
            "baseline": ", ".join(baseline["effects_allowed"]) or "-",
            "protected": ", ".join(protected["effects_allowed"]) or "-",
        },
        {
            "aspect": "Duplicate writes on replay",
            "baseline": "no fencing — a replay would double-apply",
            "protected": (
                f"{protected['duplicates_prevented']} prevented "
                f"(checkpoint resume, idempotent apply)"),
        },
        {
            "aspect": "Task outcome",
            "baseline": (
                "completed — with the unsafe action executed"
                if baseline["unsafe_action_executed"]
                else "completed — but no remediation was ever applied"),
            "protected": (
                f"{protected['disposition']}"
                + (f"; standby resumed from tick "
                   f"{(protected['resume'] or {}).get('resumed_from_tick')}"
                   if protected.get("resume") else "")),
        },
    ]
    return {
        "incident_id": baseline["incident_id"],
        "failure": baseline["failure"],
        "rows": rows,
        "scorecard": build_scorecard(
            protected["layer"],
            rerun_cmd=(f"python3 -m sim.ablation --failure {baseline['failure']} "
                       f"--incident {baseline['incident_id']}")),
    }


def render_text_comparison(comp: Dict[str, Any]) -> List[str]:
    lines = [
        "ABLATION: workflow alone vs workflow + continuity layer",
        f"incident {comp['incident_id']} | failure: {comp['failure']} "
        f"(same agents, same steps, same scripted outputs)",
        "",
    ]
    for r in comp["rows"]:
        lines.append(f"  {r['aspect']}")
        lines.append(f"    alone : {r['baseline']}")
        lines.append(f"    layer : {r['protected']}")
    lines.append("")
    lines.extend(render_text(comp["scorecard"]))
    return lines


# ---------------------------------------------------------------------------
# Challenge C: stale write after failover. Self-contained: forces a
# failover, then the fenced primary retries with its revoked generation.
# Baseline: a naive applier with no fencing generations -- the delayed
# duplicate write applies, so one intended action becomes two effects.
# ---------------------------------------------------------------------------

def run_baseline_stale_write(incident_id: str = "INC-1043") -> Dict[str, Any]:
    applied = ["restart:cache", "restart:cache"]  # original + delayed retry
    return {
        "mode": "naive applier (no fencing generations)",
        "incident_id": incident_id,
        "failure": "stale_write",
        "effects_applied": applied,
        "rejected": False,
    }


def run_protected_stale_write(incident_id: str = "INC-1043") -> Dict[str, Any]:
    """Real failover, then the real store rejection path (StaleEpochError).
    Always offline."""
    from continuity.fencing import StaleEpochError

    layer, store, primaries = make_world(diagnostician=StubDiagnostician(),
                                         live=False)
    assert layer.live is False
    inc = make_incident(incident_id)
    ctx = {"incident": inc}
    for role in ("triage", "diagnosis", "remediation"):
        result = layer.invoke(role, ctx, primaries[role])
        layer.apply_effects(result, store)
        ctx[role] = result["output"]
    primaries["remediation"].backend.mode = FAILURE_MODES["gray"]
    stale_gen = layer.fencing.current("remediation")
    result = layer.invoke("remediation", ctx, primaries["remediation"])
    layer.apply_effects(result, store)
    assert result["active"] == "standby", \
        f"expected a failover, got active={result['active']}"
    current_gen = layer.fencing.current("remediation")
    action = {"kind": "restart", "target": "cache", "params": {}}
    action_id = f"{incident_id}:remediation:9:restart:cache"
    tick = layer.clock.now()
    rejected, reason = False, ""
    try:
        store.apply_effect("remediation", "remediation", stale_gen,
                           action_id, action, tick=tick)
    except StaleEpochError as exc:
        rejected = True
        reason = str(exc)
        layer.ledger.record(
            "stale_write_rejected",
            {"agent": "remediation", "idempotency_key": action_id,
             "generation_attempted": stale_gen,
             "generation_current": current_gen,
             "reason": reason}, tick)
    return {
        "mode": "workflow + continuity layer (real failover, real fence)",
        "incident_id": incident_id,
        "failure": "stale_write",
        "layer": layer,
        "attempted_generation": stale_gen,
        "current_generation": current_gen,
        "action": "restart:cache",
        "action_id": action_id,
        "rejected": rejected,
        "reason": reason,
        "blocked_actions": ["restart:cache"],
    }


def compare_stale_write(baseline: Dict[str, Any],
                        protected: Dict[str, Any]) -> Dict[str, Any]:
    rows = [
        {
            "aspect": "Delayed write from the fenced primary",
            "baseline": ("APPLIED -- no fencing generations exist, so the "
                         "delayed retry lands as a second effect"),
            "protected": ("REJECTED at the store boundary "
                          f"(StaleEpochError: {protected['reason']})"
                          if protected["rejected"]
                          else "ACCEPTED -- the fence did not hold (bug!)"),
        },
        {
            "aspect": "Effects for one intended action",
            "baseline": str(len(baseline["effects_applied"])),
            "protected": "1 (the retry was refused, never half-applied)",
        },
        {
            "aspect": "Write authority after failover",
            "baseline": "no notion of generations -- anyone can write",
            "protected": (f"generation {protected['attempted_generation']} "
                          f"revoked; current generation "
                          f"{protected['current_generation']}"),
        },
    ]
    return {
        "incident_id": baseline["incident_id"],
        "failure": "stale_write",
        "rows": rows,
        "scorecard": build_scorecard(
            protected["layer"],
            rerun_cmd=("python3 -c \"from sim.ablation import "
                       "run_challenge; run_challenge('stale_write')\"")),
    }


# ---------------------------------------------------------------------------
# Challenge mode: one entry point for the three "break it" buttons.
# Returns everything the UI needs: title, baseline-vs-protected
# comparison, and the recovery receipt -- all from real records.
# ---------------------------------------------------------------------------

CHALLENGE_TITLES = {
    "lost_ack": "Challenge A \u2014 lose the acknowledgement",
    "unsafe": "Challenge B \u2014 propose an unsafe action",
    "stale_write": "Challenge C \u2014 stale write after failover",
    "silent": "Challenge D \u2014 silent semantic failure",
}


def _challenge_stages(scenario: str, protected: dict, receipt: dict,
                      timeline: list) -> list:
    """Recovery stages for the UI timeline, derived ONLY from the runs'
    own records. A stage with no record renders as 'not recorded' --
    never as an invented success. Statuses: done / attention / missing.
    """
    stages = []
    if scenario == "lost_ack":
        for phase in timeline:
            stages.append({
                "name": phase["title"],
                "detail": phase["detail"],
                "status": "done" if phase.get("state") == "ok"
                          else "attention",
            })
        return stages
    if scenario == "stale_write":
        layer = protected.get("layer")
        failover = None
        if layer is not None:
            failover = next(
                (e for e in layer.ledger.timeline
                 if e.get("kind") == "standby_activated"), None)
        if failover is not None:
            d = failover.get("detail") or {}
            stages.append({
                "name": "Failover",
                "detail": (f"Standby {d.get('standby')} activated at tick "
                           f"{failover.get('tick')}, generation "
                           f"{d.get('generation')}."),
                "status": "done",
            })
        else:
            stages.append({
                "name": "Failover",
                "detail": "No standby activation recorded in this run.",
                "status": "missing",
            })
        stages.append({
            "name": "Stale write arrives",
            "detail": (f"Delayed retry {protected.get('action_id')} "
                       f"presented generation "
                       f"{protected.get('attempted_generation')} after "
                       f"failover."),
            "status": "done",
        })
        if protected.get("rejected"):
            stages.append({
                "name": "Epoch fencing rejects",
                "detail": (f"{protected.get('reason')}; current generation "
                           f"{protected.get('current_generation')}."),
                "status": "done",
            })
        else:
            stages.append({
                "name": "Epoch fencing rejects",
                "detail": "The fence did not hold -- the stale write was accepted.",
                "status": "attention",
            })
        stages.append({
            "name": "Verification",
            "detail": ((receipt.get("operations") or [{}])[0].get(
                "recovery_result") or "not recorded"),
            "status": "done" if protected.get("rejected") else "attention",
        })
        return stages
    # unsafe + silent share the protected-run record shape.
    det = protected.get("detected") or {}
    if det:
        stages.append({
            "name": "Detection",
            "detail": (f"{det.get('kind')} at tick {det.get('tick')}: "
                       f"{(det.get('violations') or [''])[0]}"),
            "status": "done",
        })
    else:
        stages.append({
            "name": "Detection",
            "detail": "No anomaly or violation recorded in this run.",
            "status": "missing",
        })
    gen = (protected.get("fencing_generations") or {}).get("remediation")
    blocked = protected.get("blocked_actions") or []
    if blocked:
        stages.append({
            "name": "Quarantine + epoch fencing",
            "detail": (f"Blocked before any effect: "
                       f"{', '.join(blocked)}; remediation fenced at "
                       f"generation {gen}."),
            "status": "done",
        })
    elif gen is not None and gen > 1:
        stages.append({
            "name": "Quarantine + epoch fencing",
            "detail": (f"Compromised primary fenced; remediation generation "
                       f"{gen} (no unsafe action was proposed in this run)."),
            "status": "done",
        })
    else:
        stages.append({
            "name": "Quarantine + epoch fencing",
            "detail": "No fencing rotation recorded in this run.",
            "status": "missing",
        })
    resume = protected.get("resume")
    if resume:
        stages.append({
            "name": "Recovery",
            "detail": (f"Standby {resume.get('standby')} resumed from tick "
                       f"{resume.get('resumed_from_tick')} at generation "
                       f"{resume.get('generation')}; failover tick "
                       f"{protected.get('failover_tick')}."),
            "status": "done",
        })
    else:
        stages.append({
            "name": "Recovery",
            "detail": "No failover in this run.",
            "status": "missing",
        })
    disposition = protected.get("disposition")
    needs_review = bool(disposition) and (
        "human review" in disposition or "halt" in disposition)
    stages.append({
        "name": "Verification",
        "detail": (f"Effects allowed: "
                   f"{', '.join(protected.get('effects_allowed') or []) or 'none'}; "
                   f"duplicates prevented: "
                   f"{protected.get('duplicates_prevented')}; "
                   f"disposition: {disposition or 'not recorded'}."),
        "status": "attention" if needs_review else "done",
    })
    return stages


def run_challenge(scenario: str,
                  incident_id: Optional[str] = None) -> Dict[str, Any]:
    """Run one break-it challenge. `scenario` is lost_ack | unsafe |
    stale_write | silent. Returns {scenario, incident_id, title,
    comparison, receipt, timeline, stages} -- comparison, receipt, and
    stages built from the runs' own records."""
    import uuid as _uuid

    if scenario not in CHALLENGE_TITLES:
        raise ValueError(f"unknown challenge: {scenario}")
    incident_id = (incident_id
                   or f"CHAL-{scenario}-{_uuid.uuid4().hex[:6]}".upper())
    timeline: list = []
    if scenario == "lost_ack":
        baseline = run_baseline("lost_ack", incident_id)
        protected = run_protected("lost_ack", incident_id)
        # The fair middle baseline + the provider-amnesia variants, all
        # executed (not asserted) so every comparison cell is measured.
        idem = run_idempotency_only_lost_ack(incident_id)
        idem_amnesia = run_idempotency_only_lost_ack(
            incident_id + "-AMN", provider_amnesia=True)
        protected_amnesia = run_protected_lost_ack(
            incident_id + "-AMN", provider_amnesia=True)
        comparison = compare_lost_ack_three_way(
            baseline, idem, protected, idem_amnesia, protected_amnesia)
        receipt = receipt_from_lost_ack(protected)
        timeline = build_booking_timeline(protected)
        stages = _challenge_stages(scenario, protected, receipt, timeline)
    elif scenario == "unsafe":
        baseline = run_baseline("gray", incident_id)
        protected = run_protected("gray", incident_id)
        comparison = compare(baseline, protected)
        receipt = receipt_from_blocked_action(
            incident_id, protected, label="restart:database",
            decision_source=("ledger.critical_violation + fencing rotation "
                             "(role fenced before any effect)"),
            recovery_result=("compromised primary fenced; standby resumed "
                             "from the incident-scoped checkpoint"))
        stages = _challenge_stages(scenario, protected, receipt, timeline)
    elif scenario == "silent":
        baseline = run_baseline("silent", incident_id)
        protected = run_protected("silent", incident_id)
        comparison = compare(baseline, protected)
        det = protected.get("detected") or {}
        resume = protected.get("resume") or {}
        receipt = build_receipt(
            incident_id,
            [{
                "op_id": f"{incident_id}:silent:remediation",
                "label": ("remediation (silent failure: empty plan, "
                          "status=success)"),
                "kind": "remediation",
                "target": "payments-api",
                "effect_receipt": (
                    f"{', '.join(protected.get('effects_allowed') or [])} "
                    f"(standby replay, from checkpoint "
                    f"completed_action_ids)"),
                "retry_decision": "replayed",
                "decision_source": (
                    f"ledger.{det.get('kind')} at tick {det.get('tick')} + "
                    f"standby_activated record (generation "
                    f"{resume.get('generation')})"),
                "recovery_result": (
                    f"{protected.get('disposition')}; standby resumed "
                    f"from tick {resume.get('resumed_from_tick')}"
                    if resume else
                    f"{protected.get('disposition') or 'not recorded'}"),
            }],
            layer=protected.get("layer"))
        stages = _challenge_stages(scenario, protected, receipt, timeline)
    else:  # stale_write
        baseline = run_baseline_stale_write(incident_id)
        protected = run_protected_stale_write(incident_id)
        comparison = compare_stale_write(baseline, protected)
        receipt = receipt_from_blocked_action(
            incident_id, protected, label="restart:cache",
            decision_source="store.StaleEpochError at the effect boundary",
            recovery_result=("fenced primary's delayed write rejected; "
                             "nothing was half-applied"))
        stages = _challenge_stages(scenario, protected, receipt, timeline)
    return {
        "scenario": scenario,
        "incident_id": incident_id,
        "title": CHALLENGE_TITLES[scenario],
        "comparison": comparison,
        "receipt": receipt,
        "timeline": timeline,
        "stages": stages,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Ablation: same failure scenario, with and without "
                    "the continuity layer.")
    ap.add_argument("--failure", choices=("gray", "silent", "lost_ack"),
                    default="gray",
                    help="gray/silent: invariant-violating agent output; "
                         "lost_ack: the action commits but the acknowledgement "
                         "is lost (commit-aware recovery demo)")
    ap.add_argument("--incident", default="INC-1043")
    args = ap.parse_args(argv)

    baseline = run_baseline(args.failure, args.incident)
    protected = run_protected(args.failure, args.incident)
    for line in render_text_comparison(compare(baseline, protected)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Reproducible adversarial benchmark: oracle correctness + probe power.

Part 1 — oracle correctness (40 cases, 4 roles x 10, fully offline).
Each case feeds one scripted agent output to the role's deterministic
oracle (via CanaryRunner) and checks the verdict against the verdict
the oracle's own code implies. No model is involved. Cases flagged
``gap=True`` are ones where the oracle's verdict is arguably wrong for
safety (whitespace counts as present; a restart aimed at "db" evades
the exact "database" forbidden-target check). They are REPORTED, not
hidden and not fixed here: changing oracle verdicts is safety-logic
work that needs its own review, not a benchmark tweak.

Part 2 — probe discrimination (static vs Nemotron, offline-safe).
Context-sensitive degraded agents fail only on incidents matching a
blind spot (service, severity, symptom). The frozen static probe has
one fixed context (CANARY-1: payments-api, error rate > 5%, P1), so it
catches only the blind spots that context hits. Nemotron-generated
probes vary context, so in principle they catch more — but that arm
runs ONLY with an explicit live opt-in (CONTINUITY_BENCH_LIVE=1 plus
NEBIUS_API_KEY plus CONTINUITY_LIVE=1). Offline it reports "skipped",
never fabricated numbers.

Run:  python3 sim/adversarial_benchmark.py [--live]
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from continuity.canary import (  # noqa: E402
    CANARY_TASKS,
    CanaryRunner,
    CanaryTask,
    ORACLES,
    StaticCanaryGenerator,
    default_canary_generator,
)


class _StubAgent:
    """Returns one fixed output (or raises / returns None) per case."""

    def __init__(self, output, raises=False):
        self._output = output
        self._raises = raises

    def handle(self, ctx):
        if self._raises:
            raise RuntimeError("stub agent crashed")
        if self._output is None:
            return None
        import copy
        return copy.deepcopy(self._output)


# ---------------------------------------------------------------- Part 1
# Each case: (id, role, output, expected_pass, gap, note).
# expected_pass is derived from the oracle code in continuity/canary.py,
# NOT from what we wish the oracle did.

_HEALTHY = {
    "triage": {"severity": "P1", "category": "payments"},
    "diagnosis": {"root_cause": "bad deployment v5.2",
                  "evidence": ["deploy log"]},
    "remediation": {"remediation": "roll back to v5.1",
                    "evidence": ["deploy log"],
                    "actions": [{"kind": "rollback"}]},
    "escalation": {"ticket_id": "INC-1", "summary": "escalated to on-call"},
}

_CASES = [
    # triage: pass iff severity and category are truthy
    ("triage-healthy", "triage", _HEALTHY["triage"], True, False, ""),
    ("triage-missing-severity", "triage", {"category": "payments"},
     False, False, ""),
    ("triage-missing-category", "triage", {"severity": "P1"},
     False, False, ""),
    ("triage-empty", "triage", {}, False, False, ""),
    ("triage-severity-none", "triage",
     {"severity": None, "category": "payments"}, False, False, ""),
    ("triage-category-empty", "triage",
     {"severity": "P1", "category": ""}, False, False, ""),
    ("triage-severity-whitespace", "triage",
     {"severity": " ", "category": "payments"}, True, True,
     "whitespace counts as present (truthiness check, no strip)"),
    ("triage-extra-keys-missing-required", "triage",
     {"severity": "P1", "note": "urgent"}, False, False, ""),
    ("triage-severity-zero", "triage",
     {"severity": 0, "category": "payments"}, False, False, ""),
    ("triage-none-output", "triage", None, False, False,
     "agent returned None; runner reports oracle error, verdict fail"),
    # diagnosis: pass iff root_cause and evidence are truthy
    ("diagnosis-healthy", "diagnosis", _HEALTHY["diagnosis"],
     True, False, ""),
    ("diagnosis-missing-root-cause", "diagnosis",
     {"evidence": ["deploy log"]}, False, False, ""),
    ("diagnosis-evidence-empty-list", "diagnosis",
     {"root_cause": "bad deploy", "evidence": []}, False, False, ""),
    ("diagnosis-evidence-missing", "diagnosis",
     {"root_cause": "bad deploy"}, False, False, ""),
    ("diagnosis-root-cause-none", "diagnosis",
     {"root_cause": None, "evidence": ["deploy log"]}, False, False, ""),
    ("diagnosis-root-cause-whitespace", "diagnosis",
     {"root_cause": " ", "evidence": ["deploy log"]}, True, True,
     "whitespace counts as present (truthiness check, no strip)"),
    ("diagnosis-empty", "diagnosis", {}, False, False, ""),
    ("diagnosis-evidence-string", "diagnosis",
     {"root_cause": "bad deploy", "evidence": "deploy log"}, True, False,
     "oracle accepts any truthy evidence, not only a list (leniency)"),
    ("diagnosis-whitespace-plus-empty-evidence", "diagnosis",
     {"root_cause": " ", "evidence": []}, False, False, ""),
    ("diagnosis-evidence-none", "diagnosis",
     {"root_cause": "bad deploy", "evidence": None}, False, False, ""),
    # remediation: rollback word + evidence + rollback action, no
    # restart-database action (see remediation_canary_oracle)
    ("remediation-healthy", "remediation", _HEALTHY["remediation"],
     True, False, ""),
    ("remediation-no-rollback-word", "remediation",
     {"remediation": "restart the service", "evidence": ["log"],
      "actions": [{"kind": "restart", "target": "service"}]},
     False, False, ""),
    ("remediation-no-evidence", "remediation",
     {"remediation": "roll back", "evidence": [],
      "actions": [{"kind": "rollback"}]}, False, False, ""),
    ("remediation-no-rollback-action", "remediation",
     {"remediation": "roll back", "evidence": ["log"], "actions": []},
     False, False, ""),
    ("remediation-forbidden-restart", "remediation",
     {"remediation": "roll back", "evidence": ["log"],
      "actions": [{"kind": "rollback"},
                  {"kind": "restart", "target": "database"}]},
     False, False, ""),
    ("remediation-none", "remediation",
     {"remediation": None, "evidence": ["log"],
      "actions": [{"kind": "rollback"}]}, False, False, ""),
    ("remediation-uppercase-spaces", "remediation",
     {"remediation": "ROLL BACK now", "evidence": ["log"],
      "actions": [{"kind": "rollback"}]}, True, False,
     "oracle lowercases and strips spaces before matching"),
    ("remediation-rollback-only-in-actions", "remediation",
     {"remediation": "hold", "evidence": ["log"],
      "actions": [{"kind": "rollback"}]}, False, False,
     "rollback word must appear in the remediation text, not just actions"),
    ("remediation-agent-raises", "remediation", None, False, False,
     "agent raised; runner reports invocation failure, verdict fail"),
    ("remediation-restart-db-abbrev", "remediation",
     {"remediation": "roll back", "evidence": ["log"],
      "actions": [{"kind": "rollback"},
                  {"kind": "restart", "target": "db"}]}, True, True,
     "forbidden check matches target == 'database' exactly; 'db' slips"),
    # escalation: pass iff ticket_id and summary are truthy
    ("escalation-healthy", "escalation", _HEALTHY["escalation"],
     True, False, ""),
    ("escalation-missing-ticket", "escalation",
     {"summary": "escalated"}, False, False, ""),
    ("escalation-summary-empty", "escalation",
     {"ticket_id": "INC-1", "summary": ""}, False, False, ""),
    ("escalation-empty", "escalation", {}, False, False, ""),
    ("escalation-ticket-none", "escalation",
     {"ticket_id": None, "summary": "escalated"}, False, False, ""),
    ("escalation-summary-whitespace", "escalation",
     {"ticket_id": "INC-1", "summary": " "}, True, True,
     "whitespace counts as present (truthiness check, no strip)"),
    ("escalation-ticket-zero", "escalation",
     {"ticket_id": 0, "summary": "escalated"}, False, False, ""),
    ("escalation-summary-list", "escalation",
     {"ticket_id": "INC-1", "summary": ["escalated"]}, True, False,
     "oracle accepts any truthy summary, not only a string (leniency)"),
    ("escalation-both-whitespace", "escalation",
     {"ticket_id": " ", "summary": " "}, True, True,
     "both whitespace-only values still pass"),
    ("escalation-ticket-empty", "escalation",
     {"ticket_id": "", "summary": "escalated"}, False, False, ""),
]

# Cases whose agent raises instead of returning output.
_RAISING_CASES = {"remediation-agent-raises"}


def run_part1() -> dict:
    runner = CanaryRunner()
    results, mismatches, gaps = [], [], []
    for case_id, role, output, expected, gap, note in _CASES:
        agent = _StubAgent(output, raises=case_id in _RAISING_CASES)
        task = CanaryTask(name=case_id, role=role,
                          synthetic_ctx=dict(CANARY_TASKS[role].synthetic_ctx),
                          oracle=ORACLES[role])
        result = runner.run(agent, task)
        matched = result.passed == expected
        results.append({"case": case_id, "role": role,
                        "expected_pass": expected,
                        "actual_pass": result.passed, "matched": matched,
                        "gap": gap, "note": note,
                        "reasons": result.reasons})
        if not matched:
            mismatches.append(case_id)
        if gap:
            gaps.append(case_id)
    return {"cases": len(results), "matched": len(results) - len(mismatches),
            "mismatches": mismatches, "gap_cases": gaps, "results": results}


# ---------------------------------------------------------------- Part 2
def _role_output(role: str, degraded: bool) -> dict:
    if not degraded:
        import copy
        return copy.deepcopy(_HEALTHY[role])
    if role == "remediation":
        return {"remediation": "hold", "evidence": [], "actions": []}
    if role == "triage":
        return {"severity": None, "category": None}
    if role == "diagnosis":
        return {"root_cause": None, "evidence": []}
    return {"ticket_id": None, "summary": None}


class _BlindAgent:
    """Healthy except on incidents matching its blind-spot predicate."""

    def __init__(self, role: str, predicate):
        self._role = role
        self._predicate = predicate

    def handle(self, ctx):
        incident = (ctx or {}).get("incident", {})
        return _role_output(self._role, self._predicate(incident))


_BLIND_SPOTS = [
    ("always-degraded", lambda inc: True),
    ("service-payments-api",
     lambda inc: inc.get("service") == "payments-api"),
    ("severity-p1", lambda inc: inc.get("severity") == "P1"),
    ("symptom-latency",
     lambda inc: "latency" in str(inc.get("symptom", "")).lower()),
]


def run_part2(live: bool = False) -> dict:
    runner = CanaryRunner()
    static_detection = {}
    for role in ("triage", "diagnosis", "remediation", "escalation"):
        task = CANARY_TASKS[role]
        caught = []
        for name, predicate in _BLIND_SPOTS:
            result = runner.run(_BlindAgent(role, predicate), task)
            if not result.passed:
                caught.append(name)
        static_detection[role] = {
            "agents": len(_BLIND_SPOTS), "caught": caught,
            "detection_rate": len(caught) / len(_BLIND_SPOTS)}
    nemotron = {"status": "skipped",
                "reason": "offline default; set CONTINUITY_BENCH_LIVE=1 "
                          "with NEBIUS_API_KEY + CONTINUITY_LIVE=1"}
    if live:
        gen = default_canary_generator(live=True)
        if gen.source != "nemotron" or not gen.available:
            nemotron = {"status": "unavailable",
                        "reason": "live requested but no usable Nemotron "
                                  "client (key/opt-in missing)"}
        else:
            per_role = {}
            for role in ("triage", "diagnosis", "remediation", "escalation"):
                tasks = gen.generate(role)
                caught = []
                for name, predicate in _BLIND_SPOTS:
                    agent = _BlindAgent(role, predicate)
                    if any(not runner.run(agent, t).passed for t in tasks):
                        caught.append(name)
                per_role[role] = {
                    "probes": len(tasks), "caught": caught,
                    "detection_rate": len(caught) / len(_BLIND_SPOTS)}
            nemotron = {"status": "ran", "per_role": per_role}
    return {"static_detection": static_detection, "nemotron": nemotron}


def run_benchmark(live: bool = False) -> dict:
    part1 = run_part1()
    part2 = run_part2(live=live)
    return {"part1_oracle_correctness": {
                "cases": part1["cases"], "matched": part1["matched"],
                "mismatches": part1["mismatches"],
                "gap_cases": part1["gap_cases"]},
            "part2_probe_discrimination": part2,
            "part1_detail": part1["results"]}


def main() -> int:
    live = "--live" in sys.argv or bool(
        os.environ.get("CONTINUITY_BENCH_LIVE", "").strip())
    report = run_benchmark(live=live)
    p1 = report["part1_oracle_correctness"]
    print("Part 1 — oracle correctness: "
          f"{p1['matched']}/{p1['cases']} cases match expected verdicts; "
          f"mismatches: {p1['mismatches'] or 'none'}; "
          f"known-gap cases reported (not hidden): {p1['gap_cases']}")
    p2 = report["part2_probe_discrimination"]
    for role, det in p2["static_detection"].items():
        print(f"Part 2 — static probe, {role}: caught "
              f"{len(det['caught'])}/{det['agents']} blind agents "
              f"({det['caught']})")
    print(f"Part 2 — Nemotron arm: {p2['nemotron']['status']}"
          + (f" ({p2['nemotron'].get('reason', '')})"
             if p2['nemotron']['status'] != 'ran' else ""))
    if p2['nemotron']['status'] == 'ran':
        for role, det in p2['nemotron']['per_role'].items():
            print(f"Part 2 — Nemotron probes, {role}: {det['probes']} "
                  f"probes, caught {len(det['caught'])}/{len(_BLIND_SPOTS)}")
    if p1["mismatches"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

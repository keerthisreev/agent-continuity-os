"""Brutal proof for the Agent Continuity Layer. Plain asserts, deterministic.

Failure modes:
  A: hard death        -- remediation agent process killed
  B: silent dependency -- agent alive, LLM connection broken (THE gray beat)
  C: gray semantic     -- agent alive, returns plausible-but-useless output

Plus: healthy run (no false failover), duplicate prevention, stale-generation
fencing + safe rejoin, ledger completeness, diagnostician optionality.

Run:  python3 test_continuity.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents.triage import TriageAgent
from agents.diagnosis import DiagnosisAgent
from agents.remediation import RemediationAgent
from agents.escalation import EscalationAgent
from agents.standbys import make_standby
from continuity.adapter import ContinuityLayer
from continuity.checkpoint import STATE_ADAPTERS
from continuity.contracts import CONTRACTS
from continuity.diagnostician import (StubDiagnostician, NemotronDiagnostician,
                                      default_diagnostician)
from continuity.fencing import StaleEpochError
from continuity.sentinel import HEALTHY, FAILOVER
from continuity.standby import StandbyController
from store import IncidentStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def incident(inc_id: str, symptom: str = "error rate > 5%") -> dict:
    return {"id": inc_id, "service": "payments-api",
            "symptom": symptom, "severity": "P1"}


INCIDENTS = [incident("INC-1042"), incident("INC-1043"), incident("INC-1044")]

ROLES = ("triage", "diagnosis", "remediation", "escalation")


def make_world(diagnostician=None, live=False):
    # Tests are ALWAYS offline: live defaults to False here regardless of
    # environment, so a stray NEBIUS_API_KEY can never turn the suite into
    # live inference calls.
    layer = ContinuityLayer(diagnostician=diagnostician, live=live)
    store = IncidentStore(layer.fencing, layer.ledger)
    primaries = {
        "triage": TriageAgent(),
        "diagnosis": DiagnosisAgent(),
        "remediation": RemediationAgent(),
        "escalation": EscalationAgent(),
    }
    for role, agent in primaries.items():
        layer.register(agent.agent_id, CONTRACTS[role],
                       state_adapter=STATE_ADAPTERS[role],
                       standby=StandbyController(make_standby(role)),
                       primary=agent)
    return layer, store, primaries


def process_incident(layer, store, primaries, inc):
    """The existing 4-agent workflow, unchanged, running under the layer."""
    ctx = {"incident": inc}
    for role in ROLES:
        result = layer.invoke(role, ctx, primaries[role])
        layer.apply_effects(result, store)
        ctx[role] = result["output"]
    return ctx


def kinds(ledger):
    return [e["kind"] for e in ledger.timeline]


# ---------------------------------------------------------------------------
# 1. Healthy run: the undercover agent watches, never interferes
# ---------------------------------------------------------------------------

def test_healthy_run_no_false_failover():
    layer, store, primaries = make_world()
    for inc in INCIDENTS[:2]:
        ctx = process_incident(layer, store, primaries, inc)
        assert ctx["escalation"]["ticket_id"] == f"TICK-{inc['id']}"
    for role in ROLES:
        assert layer.sentinel.state_of(role) == HEALTHY, role
    assert layer.ledger.windows == []
    assert "failover" not in kinds(layer.ledger)
    # every incident completed cleanly through all four stages
    for inc in INCIDENTS[:2]:
        recs = [r for r in layer.ledger.stage_records if r["incident"] == inc["id"]]
        assert len(recs) == 4 and all(r["outcome"] == "ok" for r in recs), inc["id"]
    print("PASS healthy run: 2 incidents, 8 stages, zero false failovers")


# ---------------------------------------------------------------------------
# 2. Failure A: hard death of the remediation agent
# ---------------------------------------------------------------------------

def test_failure_a_hard_kill():
    layer, store, primaries = make_world()
    process_incident(layer, store, primaries, INCIDENTS[0])

    primaries["remediation"].alive = False  # kill -9 the process
    ctx = process_incident(layer, store, primaries, INCIDENTS[1])

    assert layer.sentinel.state_of("remediation") == FAILOVER
    # workflow continued: escalation still produced the ticket
    assert ctx["escalation"]["ticket_id"] == "TICK-INC-1043"
    # the remediation that ran came from the standby, exactly once
    fx = store.effects_for("INC-1043")
    assert len(fx) == 1, fx
    eff = next(iter(fx.values()))
    assert eff["agent"] == "remediation-standby" and eff["kind"] == "rollback"
    # no work lost, no duplicates
    assert layer.ledger.duplicates_prevented == 0
    assert "crash" in kinds(layer.ledger)
    print("PASS failure A (hard kill): detected, fenced, standby continued, no work lost")


# ---------------------------------------------------------------------------
# 3. Failure B: silent dependency failure -- THE gray-failure beat.
#    Agent alive, HTTP 200, output well-formed... and useless.
#    It must NOT be classified as healthy.
# ---------------------------------------------------------------------------

def test_failure_b_silent_dependency():
    layer, store, primaries = make_world()
    process_incident(layer, store, primaries, INCIDENTS[0])

    # The LLM connection breaks. The agent stays alive and degrades silently.
    primaries["remediation"].backend.mode = "llm_down"
    ctx = process_incident(layer, store, primaries, INCIDENTS[1])

    # Never classified as healthy: anomaly -> suspicion canary -> failover.
    tl = kinds(layer.ledger)
    assert "anomaly" in tl, "contract anomaly was never raised"
    canaries = [e for e in layer.ledger.timeline if e["kind"] == "suspicion_canary"]
    assert canaries and not canaries[0]["detail"]["passed"], "canary must fail"
    assert "failover" in tl
    assert layer.sentinel.state_of("remediation") == FAILOVER

    # The workflow continued via the standby with a REAL remediation.
    assert ctx["remediation"]["remediation"] is not None
    assert "rollback" in ctx["remediation"]["remediation"].lower()
    assert ctx["escalation"]["ticket_id"] == "TICK-INC-1043"

    # Affected-work map: the bad output was replayed, nothing silently kept.
    affected = layer.ledger.report()["affected_work"]
    assert affected["INC-1043"] == "replayed by standby", affected
    assert affected["INC-1042"] == "completed clean", affected

    # TSFD: first anomaly -> failover, measured in ticks.
    assert layer.ledger.report()["tsfd_ticks"] is not None
    print("PASS failure B (silent dependency): gray failure caught, "
          "never called healthy, standby replayed the work")


# ---------------------------------------------------------------------------
# 4. Failure C: gray semantic failure -- plausible, valid-shaped, wrong.
# ---------------------------------------------------------------------------

def test_failure_c_gray_semantic():
    layer, store, primaries = make_world()
    process_incident(layer, store, primaries, INCIDENTS[0])

    primaries["remediation"].backend.mode = "gray"
    ctx = process_incident(layer, store, primaries, INCIDENTS[1])

    tl = kinds(layer.ledger)
    assert "critical_violation" in tl, "business invariant violation missed"
    assert "targeted_canary" in tl
    assert "failover" in tl

    # The forbidden database restart never reached the store.
    fx = store.effects_for("INC-1043")
    assert len(fx) == 1 and next(iter(fx.values()))["kind"] == "rollback", fx

    # A plausible-but-wrong output was *attempted*: flagged for a human.
    flagged = [h["incident"] for h in layer.ledger.human_review]
    assert "INC-1043" in flagged, "gray output should be human-reviewed"

    assert ctx["escalation"]["ticket_id"] == "TICK-INC-1043"
    print("PASS failure C (gray semantic): invariant violation quarantined, "
          "database restart blocked, human flagged")


# ---------------------------------------------------------------------------
# 5. No duplicate actions: standby resumes, never redoes completed work
# ---------------------------------------------------------------------------

def test_no_duplicate_actions():
    layer, store, primaries = make_world()
    inc = incident("INC-900")
    ctx = {"incident": inc}
    for role in ("triage", "diagnosis"):
        r = layer.invoke(role, ctx, primaries[role])
        layer.apply_effects(r, store)
        ctx[role] = r["output"]

    # Primary produces a healthy plan; one action gets applied; then it dies
    # mid-stage (the classic partial-failure).
    r = layer.invoke("remediation", ctx, primaries["remediation"])
    action = r["output"]["actions"][0]
    aid = layer._action_id(inc["id"], "remediation", 0, action)
    assert store.apply_effect(r["active_agent_id"], r["role"], r["generation"],
                              aid, action) is True
    layer._checkpoints[("remediation", "INC-900")].completed_action_ids.append(aid)

    primaries["remediation"].alive = False
    r2 = layer.invoke("remediation", ctx, primaries["remediation"])
    assert r2["active"] == "standby"
    layer.apply_effects(r2, store)

    assert layer.ledger.duplicates_prevented >= 1, "standby must skip done work"
    assert list(store.applied).count(aid) == 1, "effect applied exactly once"
    print("PASS duplicates: standby resumed mid-stage, completed work not redone")


# ---------------------------------------------------------------------------
# 6. Fencing: stale generations rejected; rejoin only at a new generation
#    after probes. The shared per-role generation means the stood-down
#    standby's writes are rejected too -- no separate revoke step to forget.
# ---------------------------------------------------------------------------

def test_stale_generation_rejected_and_rejoin():
    layer, store, primaries = make_world()
    process_incident(layer, store, primaries, INCIDENTS[0])

    primaries["remediation"].alive = False
    old_generation = layer.fencing.current("remediation")
    process_incident(layer, store, primaries, INCIDENTS[1])
    assert layer.sentinel.state_of("remediation") == FAILOVER
    # The failover rotated the shared generation; this is the standby's now.
    standby_generation = layer.fencing.current("remediation")
    assert standby_generation != old_generation

    # The dead primary's generation is worthless now.
    try:
        store.apply_effect("remediation", "remediation", old_generation,
                           "X:0:k:t", {"kind": "k"})
        raise AssertionError("stale generation write was NOT rejected")
    except StaleEpochError:
        pass

    # A delayed write from the STANDBY's generation is fine while it is
    # authoritative...
    assert store.apply_effect("remediation-standby", "remediation",
                              standby_generation, "X:0:k3:t3",
                              {"kind": "k3"}) is True

    # Primary recovers: transport back, but write authority is NOT restored
    # until the semantic recovery probe passes.
    primaries["remediation"].alive = True
    primaries["remediation"].backend.mode = "healthy"
    assert layer.attempt_rejoin("remediation") is True
    assert layer.sentinel.state_of("remediation") == HEALTHY

    new_generation = layer.fencing.current("remediation")
    assert new_generation != old_generation
    assert new_generation != standby_generation
    # The old primary's generation is still dead...
    try:
        store.apply_effect("remediation", "remediation", old_generation,
                           "X:0:k:t", {"kind": "k"})
        raise AssertionError("old generation still accepted after rejoin")
    except StaleEpochError:
        pass
    # ...AND the stood-down standby's generation is dead too. This is the
    # hole the shared per-role generation closes: no revoke step needed.
    try:
        store.apply_effect("remediation-standby", "remediation",
                           standby_generation, "X:0:k4:t4", {"kind": "k4"})
        raise AssertionError("stood-down standby write was NOT rejected")
    except StaleEpochError:
        pass
    assert store.apply_effect("remediation", "remediation", new_generation,
                              "X:0:k2:t2", {"kind": "k2"}) is True

    # Next invocation routes back to the primary.
    r = layer.invoke("remediation", {"incident": incident("INC-901"),
                                     "diagnosis": {"root_cause": "bad deployment v5.2",
                                                   "evidence": ["log"], "confidence": 0.9}},
                     primaries["remediation"])
    assert r["active"] == "primary" and r["generation"] == new_generation

    # Recovery is fully recorded: window closed with a resolution.
    w = layer.ledger.windows[0]
    assert w["end_tick"] is not None and "rejoined" in w["resolution"]
    print("PASS fencing: stale generation rejected, rejoin only after probes, "
          "new generation issued, standby invalidated on rejoin")


# ---------------------------------------------------------------------------
# 6b. Checkpoint restore: the standby resumes the FAILED incident -- not a
#     stale checkpoint from another incident -- without replaying work.
# ---------------------------------------------------------------------------

def test_failover_resumes_correct_incident_checkpoint():
    layer, store, primaries = make_world()
    # INC-A completes fully: its checkpoint stays in the slot.
    process_incident(layer, store, primaries, incident("INC-A"))
    # INC-B starts: remediation produces a healthy plan, one action applied,
    # then the primary crashes MID-INCIDENT.
    ctx_b = {"incident": incident("INC-B")}
    for role in ("triage", "diagnosis"):
        r = layer.invoke(role, ctx_b, primaries[role])
        layer.apply_effects(r, store)
        ctx_b[role] = r["output"]
    r = layer.invoke("remediation", ctx_b, primaries["remediation"])
    action = r["output"]["actions"][0]
    aid = layer._action_id("INC-B", "remediation", 0, action)
    layer.apply_effects(r, store)
    assert aid in store.applied, "the action must have landed before the crash"

    primaries["remediation"].alive = False
    r2 = layer.invoke("remediation", ctx_b, primaries["remediation"])
    assert r2["active"] == "standby"

    # The standby resumed INC-B -- provably not INC-A's stale checkpoint.
    annotation = r2["output"].get("continuity")
    assert annotation is not None, "standby output must carry resume provenance"
    assert annotation["resumed_from_incident"] == "INC-B"
    assert annotation["already_completed"] == [aid]
    assert "INC-A" not in str(annotation)

    # The standby's restored output passed contract validation before any
    # side effect (invoke only returns after sentinel.check_output passes).
    layer.apply_effects(r2, store)

    # Already-completed work was NOT replayed; the effect exists exactly once.
    assert layer.ledger.duplicates_prevented >= 1
    assert list(store.applied).count(aid) == 1

    # Forensic record attributes the resume to the right incident.
    activations = [e for e in layer.ledger.timeline
                   if e["kind"] == "standby_activated"]
    assert activations[-1]["detail"]["resumed_from_incident"] == "INC-B"
    print("PASS checkpoint restore: standby resumed the failed incident's "
          "checkpoint, no replay, no cross-incident contamination")


# ---------------------------------------------------------------------------
# 6c. Concurrency: a stale write racing a generation rotation is rejected.
# ---------------------------------------------------------------------------

def test_stale_write_racing_rotation_rejected():
    import threading
    layer, store, primaries = make_world()
    old_generation = layer.fencing.current("remediation")

    # Gate the fencing check so we can land a rotation mid-authorization.
    entered = threading.Event()
    release = threading.Event()
    real_check = layer.fencing.check

    def gated_check(role, generation, tick=None):
        entered.set()
        assert release.wait(timeout=10), "test deadlock: gate never released"
        return real_check(role, generation, tick)

    errors = []

    def racing_writer():
        try:
            store.apply_effect("remediation", "remediation", old_generation,
                               "R:0:k:t", {"kind": "k"})
        except StaleEpochError as exc:
            errors.append(exc)

    layer.fencing.check = gated_check
    try:
        t = threading.Thread(target=racing_writer)
        t.start()
        assert entered.wait(timeout=10), "test deadlock: writer never arrived"
        # The rotation lands while the stale write is mid-authorization.
        layer.fencing.rotate("remediation")
        release.set()
        t.join(timeout=10)
    finally:
        layer.fencing.check = real_check

    assert len(errors) == 1, "racing stale write must be rejected, not applied"
    assert "R:0:k:t" not in store.applied

    # The new generation still writes fine.
    new_generation = layer.fencing.current("remediation")
    assert new_generation != old_generation
    assert store.apply_effect("remediation", "remediation", new_generation,
                              "R:0:k2:t2", {"kind": "k2"}) is True
    print("PASS concurrency: stale write racing a rotation is rejected; "
          "new generation writes normally")


def test_rejoin_denied_when_still_broken():
    layer, store, primaries = make_world()
    process_incident(layer, store, primaries, INCIDENTS[0])
    primaries["remediation"].alive = False
    process_incident(layer, store, primaries, INCIDENTS[1])

    # Primary "recovers" but its model is still degraded: rejoin denied.
    primaries["remediation"].alive = True
    primaries["remediation"].backend.mode = "gray"
    assert layer.attempt_rejoin("remediation") is False
    assert layer.sentinel.state_of("remediation") == FAILOVER
    # ...but a real recovery succeeds afterwards.
    primaries["remediation"].backend.mode = "healthy"
    assert layer.attempt_rejoin("remediation") is True
    print("PASS rejoin denied while still degraded; granted after real recovery")


# ---------------------------------------------------------------------------
# 7. Ledger completeness: window, affected-work map, full timeline
# ---------------------------------------------------------------------------

def test_ledger_completeness():
    layer, store, primaries = make_world()
    process_incident(layer, store, primaries, INCIDENTS[0])
    primaries["remediation"].backend.mode = "llm_down"
    process_incident(layer, store, primaries, INCIDENTS[1])

    report = layer.ledger.report()
    assert set(report) == {"failure_windows", "affected_work", "window_incidents",
                           "stage_records", "duplicates_prevented", "human_review",
                           "tsfd_ticks", "timeline"}
    w = report["failure_windows"][0]
    assert w["first_anomaly_tick"] <= w["quarantined_tick"] <= w["failover_tick"]
    assert report["affected_work"], "affected-work map must not be empty"
    needed = {"anomaly", "suspicion_canary", "failover", "standby_activated"}
    assert needed.issubset(set(kinds(layer.ledger))), kinds(layer.ledger)
    # timeline is ordered
    ticks = [e["tick"] for e in report["timeline"]]
    assert ticks == sorted(ticks)
    print("PASS ledger: window + affected-work map + ordered timeline complete")


# ---------------------------------------------------------------------------
# 8. Diagnostician optionality: deterministic verdicts with it disabled,
#    identical verdicts with the stub, Nemotron seam needs no key offline.
# ---------------------------------------------------------------------------

def test_diagnostician_disabled_and_stub_agree():
    def run_failure_b(diagnostician):
        layer, store, primaries = make_world(diagnostician=diagnostician)
        process_incident(layer, store, primaries, INCIDENTS[0])
        primaries["remediation"].backend.mode = "llm_down"
        process_incident(layer, store, primaries, INCIDENTS[1])
        return layer

    bare = run_failure_b(None)
    stubbed = run_failure_b(StubDiagnostician())
    # Identical deterministic verdicts with and without the reasoning layer.
    assert (bare.ledger.windows[0]["failover_tick"]
            == stubbed.ledger.windows[0]["failover_tick"])
    notes = [e for e in stubbed.ledger.timeline
             if e["kind"] == "diagnostician_note"]
    assert notes and all("stub-diagnosis" in n["detail"]["note"] for n in notes)
    bare_notes = [e for e in bare.ledger.timeline
                  if e["kind"] == "diagnostician_note"]
    assert not bare_notes, "disabled diagnostician must stay silent"
    print("PASS diagnostician: sentinel authoritative alone; stub only annotates")


def test_nemotron_seam_offline_safe():
    saved = os.environ.pop("NEBIUS_API_KEY", None)
    try:
        d = NemotronDiagnostician(api_key="")
        assert not d.available
        # Falls back to the stub text: no network, no key, no failure.
        note = d.diagnose("remediation", {"contract": False}, ["boom"])
        assert "stub-diagnosis" in note
        assert isinstance(default_diagnostician(), StubDiagnostician)
        # With a key present it *would* use Nemotron (never called here).
        d2 = NemotronDiagnostician(api_key="test-key-never-sent")
        assert d2.available and d2.model.startswith("nvidia/Nemotron")
    finally:
        if saved is not None:
            os.environ["NEBIUS_API_KEY"] = saved
    print("PASS nemotron seam: stub offline, live client ready for her key")


# ---------------------------------------------------------------------------

TESTS = [
    test_healthy_run_no_false_failover,
    test_failure_a_hard_kill,
    test_failure_b_silent_dependency,
    test_failure_c_gray_semantic,
    test_no_duplicate_actions,
    test_stale_generation_rejected_and_rejoin,
    test_failover_resumes_correct_incident_checkpoint,
    test_stale_write_racing_rotation_rejected,
    test_rejoin_denied_when_still_broken,
    test_ledger_completeness,
    test_diagnostician_disabled_and_stub_agree,
    test_nemotron_seam_offline_safe,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

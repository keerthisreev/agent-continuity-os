"""Tests for the SQLite operation journal and the lost-ack scenario.

Covers: intent/commit/reconcile lifecycle, the REUSE path (same key +
same full operation -> existing receipt, no duplicate effect), the
CONFLICT path (same key + different operation -> IdempotencyConflict,
never silent reuse), key-mismatch rejection (same key, different logical
op -> IdempotencyKeyMismatch), the canonical fingerprint covering
service/op/target/params, crash recovery (close/reopen -> reconcile
returns the committed receipt), the agent/gateway information boundary
(the agent never holds the receipt; the retry reconstructs it from
SQLite), baseline-vs-protected (naive retry double-books; journal
discipline books once), and the recovery receipt (every field from real
records). Deterministic, no randomness, no network.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from continuity.journal import (DECISION_UNVERIFIED_NEEDS_REVIEW,
                                IdempotencyConflict, IdempotencyKeyMismatch,
                                OperationJournal, _fingerprint)
from continuity.fencing import StaleEpochError
from continuity.diagnostician import StubDiagnostician
from test_continuity import make_world
from sim.lost_ack import (AcknowledgementLost, ContinuityGateway,
                          SimulatedProvider, run_baseline_lost_ack,
                          run_protected_lost_ack)
from sim.receipt import NOT_RECORDED, receipt_from_lost_ack, render_text_lines


def _tmp_journal() -> OperationJournal:
    fd, path = tempfile.mkstemp(prefix="test-journal-", suffix=".db")
    os.close(fd)
    return OperationJournal(path)


def _ride_op(**over):
    """Canonical ride-reserve operation; override any field."""
    op = {"service": "ride-provider", "op": "reserve",
          "target": "airport-pickup", "params": {"time": "06:30"}}
    op.update(over)
    return op


def test_fingerprint_canonical():
    base = _ride_op(params={"b": 1, "a": 2})
    a = _fingerprint(base)
    reordered = {"params": {"a": 2, "b": 1}, "target": "airport-pickup",
                 "op": "reserve", "service": "ride-provider"}
    b = _fingerprint(reordered)
    c = _fingerprint(_ride_op(params={"a": 2, "b": 3}))
    assert a == b, "key order must not change the fingerprint"
    assert a != c, "different params must fingerprint differently"
    print("PASS journal: canonical operation fingerprint")


def test_fingerprint_covers_full_operation():
    """Same key + params for a different target/service/op is a DIFFERENT
    operation: it fingerprints differently and record_intent refuses to
    conflate it with the committed one."""
    j = _tmp_journal()
    try:
        j.record_intent("op-a", "key-1", **_ride_op())
        j.record_commit("op-a", {"booking_id": "RIDE-1"})
        for field, value in (("target", "downtown"),
                             ("op", "cancel"),
                             ("service", "other-provider")):
            other = _ride_op(**{field: value})
            assert _fingerprint(other) != _fingerprint(_ride_op()), \
                f"fingerprint must distinguish {field}"
            try:
                j.record_intent("op-a", "key-1", **other)
            except IdempotencyConflict:
                pass
            else:
                raise AssertionError(
                    f"expected IdempotencyConflict for different {field}")
        # The original record is untouched by the refused attempts.
        rec = j.get("op-a")
        assert rec["status"] == "committed", rec
        assert rec["receipt"] == {"booking_id": "RIDE-1"}, rec
    finally:
        j.close()
    print("PASS journal: fingerprint covers service/op/target/params")


def test_key_reuse_across_operations_rejected():
    """The same idempotency key presented for a DIFFERENT logical operation
    is rejected: keys are never shared across operations."""
    j = _tmp_journal()
    try:
        j.record_intent("op-a", "key-1", **_ride_op())
        try:
            j.record_intent("op-b", "key-1", **_ride_op())
        except IdempotencyKeyMismatch as exc:
            assert exc.existing_op_id == "op-a", exc
            assert isinstance(exc, IdempotencyConflict), \
                "mismatch must stay catchable as IdempotencyConflict"
            assert "op-b" in str(exc) and "op-a" in str(exc), str(exc)
        else:
            raise AssertionError("expected IdempotencyKeyMismatch")
        # op-a's record is untouched.
        assert j.get("op-a")["status"] == "pending"
        assert j.get("op-b") is None
    finally:
        j.close()
    print("PASS journal: key shared across operations -> rejected")


def test_agent_never_saw_receipt():
    """The flagship proof, structurally enforced. The gateway raises
    AcknowledgementLost (which carries no receipt data); the journal is
    then closed and reloaded from SQLite; the retry's receipt is
    reconstructed from the persisted record. The agent never held the
    original: booking count stays one, the provider is never re-hit."""
    fd, path = tempfile.mkstemp(prefix="test-gateway-", suffix=".db")
    os.close(fd)
    provider = SimulatedProvider()
    journal = OperationJournal(path)
    layer, _, _ = make_world(diagnostician=StubDiagnostician(), live=False)
    gateway = ContinuityGateway(provider, journal, layer.fencing,
                                "remediation")
    op = {"op_id": "op-gw", "key": "gw-key", **_ride_op()}
    agent_receipt = None  # everything the agent ever holds
    try:
        gateway.execute(op, lose_ack=True)
        raise AssertionError("lose_ack=True must raise, not return")
    except AcknowledgementLost as lost:
        assert not any(isinstance(a, dict) for a in lost.args), \
            "AcknowledgementLost must not carry receipt data"
        assert agent_receipt is None
    assert provider.calls == 1, provider.calls
    assert len(provider.created_receipts) == 1
    # Independent read of what was persisted before the "restart".
    persisted = OperationJournal(path).get("op-gw")
    assert persisted["status"] == "unknown", persisted
    assert persisted["receipt"] is not None
    # Gateway "restart": nothing in memory survives; the retry is served
    # from durable state.
    journal.close()
    journal2 = OperationJournal(path)
    gateway2 = ContinuityGateway(provider, journal2, layer.fencing,
                                 "remediation")
    try:
        result = gateway2.execute(op)
        assert result["reused"] is True, result
        assert result["provider_call"] is False, result
        assert provider.calls == 1, "retry must not re-hit the provider"
        assert result["receipt"] == persisted["receipt"], \
            "retried receipt must equal the persisted record"
        agent_receipt = result["receipt"]
        assert agent_receipt["effect_id"].startswith("RESERVE-")
    finally:
        journal2.close()
        os.unlink(path)
    print("PASS gateway: agent never saw the receipt; retry from SQLite")


def test_gateway_rejects_stale_generation_before_provider():
    """A fenced (rotated) agent is rejected BEFORE the provider executes:
    no downstream effect is created and no journal record is written.
    Fencing is the authorization boundary; idempotency + reconciliation
    stay separate protections."""
    layer, _, _ = make_world(diagnostician=StubDiagnostician(), live=False)
    fd, path = tempfile.mkstemp(prefix="test-fence-", suffix=".db")
    os.close(fd)
    provider = SimulatedProvider()
    journal = OperationJournal(path)
    try:
        gateway = ContinuityGateway(provider, journal, layer.fencing,
                                    "remediation")
        # Failover rotates the generation: this gateway's agent is stale.
        layer.fencing.rotate("remediation", layer.clock.tick())
        op = {"op_id": "op-fenced", "key": "fence-key", **_ride_op()}
        try:
            gateway.execute(op)
        except StaleEpochError:
            pass
        else:
            raise AssertionError(
                "expected StaleEpochError before provider dispatch")
        assert provider.calls == 0, \
            "stale agent must not reach the provider"
        assert provider.created_receipts == []
        assert journal.get("op-fenced") is None, \
            "rejected op leaves no journal record"
    finally:
        journal.close()
        os.unlink(path)
    print("PASS gateway: stale generation rejected before provider dispatch")


def test_full_restart_with_provider_state_lost_needs_review():
    """The strongest claim, scoped honestly. Simulates a FULL restart: the
    journal survives on disk, but the provider's in-memory state is gone.
    The retry must NOT blindly re-book (a fresh provider would not dedupe
    the key) and must NOT present the persisted receipt as a confirmed
    outcome: it surfaces needs-review instead."""
    layer, _, _ = make_world(diagnostician=StubDiagnostician(), live=False)
    fd, path = tempfile.mkstemp(prefix="test-restart-", suffix=".db")
    os.close(fd)
    provider = SimulatedProvider()
    journal = OperationJournal(path)
    gateway = ContinuityGateway(provider, journal, layer.fencing,
                                "remediation")
    op = {"op_id": "op-restart", "key": "restart-key", **_ride_op()}
    try:
        gateway.execute(op, lose_ack=True)
        raise AssertionError("lose_ack=True must raise, not return")
    except AcknowledgementLost:
        pass
    assert len(provider.created_receipts) == 1
    # FULL restart: journal reloaded from disk, provider state GONE.
    journal.close()
    journal2 = OperationJournal(path)
    fresh_provider = SimulatedProvider()
    gateway2 = ContinuityGateway(fresh_provider, journal2, layer.fencing,
                                 "remediation")
    try:
        result = gateway2.execute(op)
        assert result["reused"] is True, result
        assert result["needs_review"] is True, result
        assert result["verified"] is False, result
        # Nothing was blindly re-booked.
        assert fresh_provider.calls == 0, fresh_provider.calls
        assert fresh_provider.created_receipts == []
        # The journal records the honest outcome, not a fake success.
        rec = journal2.get("op-restart")
        assert rec["recovery_decision"] == DECISION_UNVERIFIED_NEEDS_REVIEW, \
            rec
    finally:
        journal2.close()
        os.unlink(path)
    print("PASS restart: provider state lost -> needs-review, no blind re-book")


def test_provider_rejects_stale_fencing_token():
    """Downstream contract: the provider validates the fencing token on
    every call. A rotation landing between the gateway's pre-check and
    the provider's execution is still rejected -- no effect recorded."""
    layer, _, _ = make_world(diagnostician=StubDiagnostician(), live=False)
    provider = SimulatedProvider(fencing=layer.fencing, role="remediation")
    token = layer.fencing.current("remediation")
    _, created = provider.execute("ride", "tok-key", {"x": 1},
                                  fencing_token=token)
    assert created is True
    # A rotation lands (e.g. failover) after the gateway's pre-check.
    layer.fencing.rotate("remediation", layer.clock.tick())
    try:
        provider.execute("ride", "tok-key-2", {"x": 2}, fencing_token=token)
        raise AssertionError("expected StaleEpochError for stale token")
    except StaleEpochError:
        pass
    assert provider.calls == 1, provider.calls
    assert len(provider.created_receipts) == 1
    # Fail-closed on a missing token too.
    try:
        provider.execute("ride", "tok-key-3", {"x": 3})
        raise AssertionError("expected StaleEpochError for missing token")
    except StaleEpochError:
        pass
    assert provider.calls == 1, provider.calls
    print("PASS provider: stale/missing fencing token rejected, no effect")


def test_unverified_booking_halts_dependent_steps():
    """End-to-end: when the provider can't verify the persisted receipt,
    the workflow halts -- the unconfirmed booking is not recorded locally,
    dependent steps (calendar, notify) never run, and the receipt says
    needs-review instead of narrating a completed booking."""
    result = run_protected_lost_ack(incident_id="RIDE-HALT",
                                    provider_amnesia=True)
    assert result["halted"] is True, result
    assert [s["step"]["name"] for s in result["steps"]] == ["reserve_ride"]
    ride = result["steps"][0]
    assert ride["needs_review"] is True, ride
    assert ride["verified"] is False, ride
    # The fresh provider recorded no effects at all.
    assert result["ride_bookings"] == 0, result
    assert result["provider_calls"] == 0, result
    # The receipt flags needs-review, not a completed booking.
    from sim.receipt import receipt_from_lost_ack
    receipt = receipt_from_lost_ack(result)
    assert len(receipt["operations"]) == 1, receipt["operations"]
    assert receipt["operations"][0]["retry_decision"] == "needs-review"
    print("PASS halt: unverified booking stops the workflow, needs-review")


def test_idempotency_only_matches_layer_on_healthy_lost_ack():
    """Honest headline, part 1: when the provider honors its key, the
    idempotency-only baseline prevents the duplicate just like the full
    layer. The layer must not pretend otherwise."""
    from sim.lost_ack import run_idempotency_only_lost_ack
    idem = run_idempotency_only_lost_ack(incident_id="IDEM-OK")
    protected = run_protected_lost_ack(incident_id="PROT-OK")
    assert idem["ride_bookings"] == 1, idem
    assert idem["duplicate_effects"] == 0, idem
    assert protected["ride_bookings"] == 1, protected
    assert protected["ride_bookings"] - 1 == 0, protected
    print("PASS idem: healthy provider -> no duplicate, matches the layer")


def test_idempotency_only_duplicates_when_provider_loses_state():
    """Honest headline, part 2: the dedupe table is the provider's memory.
    Lose it and the retry duplicates; the agent-side journal survives, so
    the full layer halts instead of duplicating."""
    from sim.lost_ack import run_idempotency_only_lost_ack
    idem = run_idempotency_only_lost_ack(incident_id="IDEM-AMN",
                                         provider_amnesia=True)
    protected = run_protected_lost_ack(incident_id="PROT-AMN",
                                       provider_amnesia=True)
    assert idem["ride_bookings"] == 2, idem
    assert idem["duplicate_effects"] == 1, idem
    assert protected["halted"] is True, protected
    assert protected["ride_bookings"] == 0, protected
    print("PASS idem: amnesia -> duplicate; layer -> halt, needs-review")


def test_three_way_comparison_every_cell_measured():
    """The comparison table is built from executed runs, not assertions."""
    from sim.lost_ack import (compare_lost_ack_three_way,
                              run_baseline_lost_ack,
                              run_idempotency_only_lost_ack)
    baseline = run_baseline_lost_ack("T3-B")
    idem = run_idempotency_only_lost_ack("T3-I")
    protected = run_protected_lost_ack("T3-P")
    idem_amnesia = run_idempotency_only_lost_ack("T3-IA",
                                                 provider_amnesia=True)
    protected_amnesia = run_protected_lost_ack("T3-PA",
                                               provider_amnesia=True)
    cmp = compare_lost_ack_three_way(baseline, idem, protected,
                                     idem_amnesia, protected_amnesia)
    assert cmp["three_way"] is True
    assert len(cmp["rows"]) == 5, cmp["rows"]
    for row in cmp["rows"]:
        assert {"aspect", "baseline", "idempotency_only",
                "protected"} <= set(row), row
    # The amnesia row quotes the measured numbers.
    amnesia_row = [r for r in cmp["rows"]
                   if "loses its state" in r["aspect"]][0]
    assert "2 bookings" in amnesia_row["idempotency_only"]
    assert "halted=True" in amnesia_row["protected"]
    print("PASS three-way: 5 aspects, every cell from real runs")


def test_intent_commit_lifecycle():
    j = _tmp_journal()
    try:
        rec = j.record_intent("op-1", "key-1", service="ride-provider",
                              op="reserve", target="airport-pickup",
                              params={"ride": "AUS"})
        assert rec["status"] == "pending", rec
        rec = j.record_commit("op-1", {"booking_id": "RIDE-1"})
        assert rec["status"] == "committed", rec
        assert rec["receipt"] == {"booking_id": "RIDE-1"}, rec
    finally:
        j.close()
    print("PASS journal: intent -> commit lifecycle")


def test_retry_same_key_same_payload_reuses_receipt():
    """(a) The lost-ack core: retry of the same logical op reuses the
    committed receipt; the provider is never hit twice."""
    j = _tmp_journal()
    provider = SimulatedProvider()
    try:
        op, key = "op-ride", "ride-key"
        params = {"pickup": "home", "dropoff": "AUS"}
        j.record_intent(op, key, service="ride-provider", op="reserve",
                        target="airport-pickup", params=params)
        receipt, created = provider.execute("reserve", key, params)
        assert created is True
        j.record_commit(op, receipt)
        # SIMULATED: the acknowledgement is lost.
        j.mark_unknown(op)
        # The agent retries the SAME logical op: journal first.
        again = j.record_intent(op, key, service="ride-provider",
                                op="reserve", target="airport-pickup",
                                params=params)
        assert again["status"] == "unknown", again
        assert again["receipt"] == receipt, "existing receipt must be returned"
        final = j.reconcile(op)
        assert final["status"] == "reconciled", final
        assert final["receipt"] == receipt, final
        assert final["recovery_decision"] == \
            "reconciled_ambiguous_commit__receipt_reused"
        # No second provider call happened on the retry path:
        assert provider.calls == 1, provider.calls
        assert len(provider.created_receipts) == 1
    finally:
        j.close()
    print("PASS journal: same key+payload retry reuses receipt, single effect")


def test_same_key_different_payload_is_conflict():
    """(b) Same key, different operation: loud conflict, original untouched."""
    j = _tmp_journal()
    try:
        j.record_intent("op-1", "key-1", service="ride-provider",
                        op="reserve", target="airport-pickup",
                        params={"ride": "AUS"})
        j.record_commit("op-1", {"booking_id": "RIDE-1"})
        try:
            j.record_intent("op-1", "key-1", service="ride-provider",
                            op="reserve", target="airport-pickup",
                            params={"ride": "DFW"})
        except IdempotencyConflict as exc:
            assert exc.idempotency_key == "key-1"
        else:
            raise AssertionError("expected IdempotencyConflict")
        rec = j.get("op-1")
        assert rec["status"] == "committed", rec
        assert rec["receipt"] == {"booking_id": "RIDE-1"}, rec
        assert rec["payload_fingerprint"] == _fingerprint(
            _ride_op(params={"ride": "AUS"})), rec
    finally:
        j.close()
    print("PASS journal: same key + different payload -> conflict, never silent")


def test_crash_recovery_close_reopen():
    """(c) Simulated process restart: records survive close/reopen and
    reconcile returns the committed receipt."""
    fd, path = tempfile.mkstemp(prefix="test-crash-", suffix=".db")
    os.close(fd)
    j = OperationJournal(path)
    receipt = {"booking_id": "RIDE-7", "status": "confirmed"}
    j.record_intent("op-x", "key-x", service="ride-provider", op="reserve",
                    target="airport-pickup", params={"ride": "AUS"})
    j.record_commit("op-x", receipt)
    j.close()
    # "Restart": a brand-new connection to the same file.
    j2 = OperationJournal(path)
    try:
        rec = j2.reconcile("op-x")
        assert rec["status"] == "reconciled", rec
        assert rec["receipt"] == receipt, rec
        assert rec["recovery_decision"] == "reuse_committed_receipt", rec
    finally:
        j2.close()
        os.unlink(path)
    print("PASS journal: state survives close/reopen (crash recovery)")


def test_reconcile_without_receipt_orders_provider_check():
    j = _tmp_journal()
    try:
        j.record_intent("op-n", "key-n", service="notify-svc", op="notify",
                        target="partner", params={"x": 1})
        j.mark_unknown("op-n")  # ack lost before any commit was recorded
        rec = j.reconcile("op-n")
        assert rec["receipt"] is None, rec
        assert rec["recovery_decision"] == \
            "no_recorded_effect__verify_provider_before_retry", rec
    finally:
        j.close()
    print("PASS journal: unknown with no receipt -> verify provider, never blind retry")


def test_baseline_double_applies_protected_single():
    """(d) End-to-end: naive retry books twice; journal discipline books once."""
    baseline = run_baseline_lost_ack("INC-J1")
    protected = run_protected_lost_ack("INC-J2")
    try:
        assert baseline["ride_bookings"] == 2, baseline
        assert baseline["duplicate_effects"] == 1, baseline
        assert protected["ride_bookings"] == 1, protected
        assert protected["provider_calls"] == 3, protected  # no 2nd ride call
        reused = [s for s in protected["steps"] if s.get("reused")]
        assert len(reused) == 1, protected["steps"]
        assert reused[0]["receipt"] == reused[0]["receipt"]
        # The journal agrees with the provider: one effect, receipt on file.
        rec = protected["journal"].get(reused[0]["op_id"])
        assert rec["status"] == "reconciled", rec
        assert rec["receipt"] == reused[0]["receipt"], rec
    finally:
        protected["journal"].close()
    print("PASS scenario: baseline double-books, protected books exactly once")


def test_store_backstop_refuses_duplicate_key():
    """Belt and suspenders: even if the agent ignored the journal, the
    store's idempotent apply refuses the same action id twice."""
    protected = run_protected_lost_ack("INC-J3")
    try:
        store, layer = protected["store"], protected["layer"]
        first = protected["steps"][0]
        action = {"kind": "reserve", "target": "airport-pickup", "params": {}}
        again = store.apply_effect(
            "sim-user-task", "remediation",
            protected["fencing_generation"], first["key"], action,
            tick=layer.clock.now())
        assert again is False, "store must refuse the duplicate action id"
    finally:
        protected["journal"].close()
    print("PASS store: duplicate action id refused even past the journal")


def test_receipt_fields_come_from_records():
    protected = run_protected_lost_ack("INC-J4")
    try:
        receipt = receipt_from_lost_ack(protected)
        assert receipt["incident_id"] == "INC-J4"
        assert len(receipt["operations"]) == 3, receipt["operations"]
        by_label = {o["label"]: o for o in receipt["operations"]}
        ride = by_label["Reserve airport pickup"]
        assert ride["retry_decision"] == "reused", ride
        assert ride["effect_receipt"]["effect_id"].startswith("RESERVE-"), ride
        assert ride["effect_receipt"] == \
            protected["journal"].get(
                protected["steps"][0]["op_id"])["receipt"]
        cal = by_label["Add pickup to shared calendar"]
        assert cal["retry_decision"] == \
            "committed first try (no retry needed)", cal
        # Global fields are derived, not hard-coded.
        assert receipt["checkpoint"] != NOT_RECORDED
        assert receipt["fencing"]["stale_writes_rejected"] == 0
        assert receipt["fencing"]["current_generations"]["remediation"] == \
            protected["fencing_generation"]
        assert receipt["final_state"]["disposition"] == "completed clean", \
            receipt["final_state"]
        lines = render_text_lines(receipt)
        assert any("Reserve airport pickup" in ln for ln in lines)
        assert any("reused" in ln for ln in lines)
    finally:
        protected["journal"].close()
    print("PASS receipt: every field read off journal/ledger/store")


def test_journal_stays_offline_with_key_set():
    old = os.environ.get("MODEL_API_KEY")
    old_live = os.environ.get("CONTINUITY_LIVE")
    os.environ["MODEL_API_KEY"] = "test-only-fake-key"
    os.environ.pop("CONTINUITY_LIVE", None)
    try:
        protected = run_protected_lost_ack("INC-J5")
        try:
            assert protected["layer"].live is False
            assert protected["ride_bookings"] == 1
        finally:
            protected["journal"].close()
    finally:
        if old is None:
            os.environ.pop("MODEL_API_KEY", None)
        else:
            os.environ["MODEL_API_KEY"] = old
        if old_live is not None:
            os.environ["CONTINUITY_LIVE"] = old_live
    print("PASS offline: key in env cannot flip the journal scenario online")


def test_no_banned_language_in_new_files():
    # Built dynamically so this file's own source doesn't contain the term.
    banned = "2" + "0k"
    new_files = ["continuity/journal.py", "sim/lost_ack.py",
                 "sim/receipt.py", "sim/ablation.py", "test_journal.py",
                 "dashboard/app.py", "sim/alexa_plus_sim.py"]
    root = os.path.dirname(os.path.abspath(__file__))
    for rel in new_files:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            text = fh.read().lower()
        if rel == "test_journal.py":
            # This test's own references to the banned term don't count.
            text = text.replace(banned, "")
        assert banned not in text, \
            f"{rel} contains banned language"
    print("PASS honesty: no banned language in new/changed files")


TESTS = [
    test_fingerprint_canonical,
    test_fingerprint_covers_full_operation,
    test_key_reuse_across_operations_rejected,
    test_agent_never_saw_receipt,
    test_gateway_rejects_stale_generation_before_provider,
    test_full_restart_with_provider_state_lost_needs_review,
    test_provider_rejects_stale_fencing_token,
    test_unverified_booking_halts_dependent_steps,
    test_idempotency_only_matches_layer_on_healthy_lost_ack,
    test_idempotency_only_duplicates_when_provider_loses_state,
    test_three_way_comparison_every_cell_measured,
    test_intent_commit_lifecycle,
    test_retry_same_key_same_payload_reuses_receipt,
    test_same_key_different_payload_is_conflict,
    test_crash_recovery_close_reopen,
    test_reconcile_without_receipt_orders_provider_check,
    test_baseline_double_applies_protected_single,
    test_store_backstop_refuses_duplicate_key,
    test_receipt_fields_come_from_records,
    test_journal_stays_offline_with_key_set,
    test_no_banned_language_in_new_files,
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
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} journal tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

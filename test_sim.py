"""Tests for the simulated Alexa+ conversational experience.

Drives SimSession programmatically through the full beat -- initiate,
interrupt with a gray/silent failure, query status, resume -- against the
REAL continuity engine (no engine mocks). Also covers the ablation
runner (baseline executes the unsafe action the layer blocks) and the
scorecard (every number read off a real ledger).
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sim.alexa_plus_sim import SimSession, parse_request
from sim.ablation import run_baseline, run_protected, compare
from sim.scorecard import build_scorecard, bucketize


def _start_continue_clean(session, prompt):
    """Start a task and run it to completion with no failure injected."""
    session.handle(prompt)
    return session.handle("continue")


def test_nlu_conversational_prompt_shape():
    p = parse_request("Investigate incident INC-1043 and prepare a "
                      "remediation plan. Don't execute an unsafe action.")
    assert p["intent"] == "start_task", p
    assert p["task"]["incident_id"] == "INC-1043", p
    assert p["task"]["service"] == "payments-api", p
    print("PASS nlu: conversational prompt -> start_task INC-1043 payments-api")


def test_nlu_all_task_specs():
    p = parse_request("Check the checkout service error rate.")
    assert p["intent"] == "start_task" and p["task"]["service"] == "checkout-api", p
    p = parse_request("Probe the orders database for flaky dependencies.")
    assert p["intent"] == "start_task" and p["task"]["service"] == "orders-db", p
    print("PASS nlu: checkout + database specs map deterministically")


def test_nlu_control_intents():
    assert parse_request("inject a gray failure") == {
        "intent": "inject", "failure": "gray"}
    assert parse_request("inject a silent failure")["failure"] == "silent"
    assert parse_request("continue")["intent"] == "continue"
    assert parse_request("what happened to my tasks?")["intent"] == "report"
    assert parse_request("run the ablation")["intent"] == "ablation"
    assert parse_request("reset")["intent"] == "reset"
    assert parse_request("tell me a joke")["intent"] == "help"
    print("PASS nlu: control intents (inject/continue/report/ablation/reset/help)")


def test_session_stays_offline_with_key_set():
    old = os.environ.get("MODEL_API_KEY")
    old_live = os.environ.get("CONTINUITY_LIVE")
    os.environ["MODEL_API_KEY"] = "test-only-fake-key"
    os.environ.pop("CONTINUITY_LIVE", None)
    try:
        s = SimSession()
        assert s.layer.live is False, "sim must stay offline even with a key set"
        s.handle("Check the checkout service error rate.")
        s.handle("continue")
        assert s.layer.live is False
    finally:
        if old is None:
            os.environ.pop("MODEL_API_KEY", None)
        else:
            os.environ["MODEL_API_KEY"] = old
        if old_live is not None:
            os.environ["CONTINUITY_LIVE"] = old_live
    print("PASS offline: key in env cannot flip the sim online")


def test_full_beat_gray_failure():
    s = SimSession()
    _start_continue_clean(s, "Check the checkout service error rate.")
    msgs = s.handle("Investigate incident INC-1043 and prepare a "
                    "remediation plan. Don't execute an unsafe action.")
    assert any(m["kind"] == "text" and "awaiting" in m["text"].lower()
               or "continue" in m.get("text", "").lower() for m in msgs)
    msgs = s.handle("inject a gray failure")
    assert s.tasks[-1]["armed"] == "gray"
    msgs = s.handle("continue")
    kinds = [m["kind"] for m in msgs]
    assert "contrast" in kinds, f"missing visceral contrast beat: {kinds}"
    assert "resume" in kinds, f"missing resume proof beat: {kinds}"

    task = s.tasks[-1]
    assert task["failover"] is not None, "no failover recorded"
    inc_id = task["incident"]["id"]
    assert inc_id == "INC-1043"

    # Checkpoint provenance: incident-scoped, resume not restart.
    pkg = s.layer.checkpoint_package("remediation", inc_id)
    assert pkg is not None and pkg.incident_id == inc_id
    prov = None
    for stage in reversed(task["stages"]):
        prov = (stage["output"] or {}).get("continuity")
        if prov:
            break
    assert prov is not None, "standby output missing continuity provenance"
    assert prov["resumed_from_incident"] == inc_id
    assert prov["already_completed"] == [f"{inc_id}:remediation:0:rollback:payments-api"]

    # Fencing + idempotency did their jobs.
    assert s.layer.fencing.current("remediation") == 2, "generation not rotated"
    assert s.layer.ledger.duplicates_prevented >= 1, "no duplicate prevented"
    assert s.layer.ledger.affected_work()[inc_id] == "human review"

    # The contrast beat names both sides from the real ledger.
    contrast = next(m for m in msgs if m["kind"] == "contrast")
    assert any("ALIVE" in line for line in contrast["outside"]["lines"])
    assert any("VIOLATED" in line or "FAILED" in line
               for line in contrast["sentinel"]["lines"])
    print("PASS beat: gray failure -> contrast + fenced + checkpoint resume + "
          "duplicates prevented")


def test_silent_failure_recovered_via_standby():
    s = SimSession()
    s.handle("Probe the orders database for flaky dependencies.")
    s.handle("inject a silent failure")
    s.handle("continue")
    inc_id = s.tasks[-1]["incident"]["id"]
    disp = s.layer.ledger.affected_work()[inc_id]
    assert disp == "replayed by standby", f"unexpected disposition: {disp}"
    assert s.layer.fencing.current("remediation") == 2
    print("PASS beat: silent failure -> replayed by standby (middle bucket)")


def test_what_happened_three_buckets():
    s = SimSession()
    _start_continue_clean(s, "Check the checkout service error rate.")
    checkout_id = s.tasks[-1]["incident"]["id"]
    s.handle("Probe the orders database for flaky dependencies.")
    s.handle("inject a silent failure")
    s.handle("continue")
    db_id = s.tasks[-1]["incident"]["id"]
    s.handle("Investigate incident INC-1043 and prepare a remediation plan.")
    s.handle("inject a gray failure")
    s.handle("continue")

    msgs = s.handle("what happened to my tasks?")
    assert len(msgs) == 1 and msgs[0]["kind"] == "report", msgs
    rep = msgs[0]
    b = rep["buckets"]
    assert b["completed_clean"] == [checkout_id], b
    assert b["recovered_via_standby"] == [db_id], b
    assert b["needs_human_review"] == ["INC-1043"], b
    assert "INC-1043" in rep["review_reasons"], "missing human-review reason"
    assert "fenced" in rep["narrative"], "narrative should mention fencing"
    score = "\n".join(rep["scorecard_lines"])
    assert "TSFD" in score and "duplicate writes prevented" in score.lower()
    assert "python3 -m sim.ablation" in score, "scorecard must carry rerun cmd"
    print("PASS report: three buckets populated + scorecard + rerun command")


def test_checkpoint_incident_scoped():
    s = SimSession()
    _start_continue_clean(s, "Check the checkout service error rate.")
    first_id = s.tasks[-1]["incident"]["id"]
    s.handle("Investigate incident INC-1043 and prepare a remediation plan.")
    s.handle("inject a gray failure")
    s.handle("continue")
    keys = list(s.layer._checkpoints.keys())
    assert ("remediation", first_id) in keys
    assert ("remediation", "INC-1043") in keys
    for (agent_id, inc_id), pkg in s.layer._checkpoints.items():
        assert pkg.incident_id == inc_id, "checkpoint slot holds wrong incident"
    print("PASS checkpoints: keyed by (agent, incident), no cross-talk")


def test_ablation_baseline_executes_what_layer_blocks():
    baseline = run_baseline("gray", "INC-1043")
    assert baseline["unsafe_action_executed"] is True
    assert "restart:database" in baseline["effects_allowed"]
    assert baseline["detected"] is None

    protected = run_protected("gray", "INC-1043")
    assert "restart:database" in protected["blocked_actions"]
    assert "restart:database" not in protected["effects_allowed"]
    assert "rollback:payments-api" in protected["effects_allowed"]
    assert protected["detected"]["kind"] == "critical_violation"
    assert protected["duplicates_prevented"] >= 1

    comp = compare(baseline, protected)
    assert len(comp["rows"]) == 5
    unsafe_row = next(r for r in comp["rows"]
                      if r["aspect"] == "Invariant-violating action")
    assert "EXECUTED" in unsafe_row["baseline"]
    assert "BLOCKED" in unsafe_row["protected"]
    card = comp["scorecard"]
    assert card["tsfd_ticks"] == 0
    assert card["rerun"] == ("python3 -m sim.ablation --failure gray "
                             "--incident INC-1043")
    print("PASS ablation: baseline executes restart:database; layer blocks it; "
          "scorecard from the real ledger")


def test_fence_test_rejects_stale_write():
    s = SimSession()
    s.handle("Investigate incident INC-1043 and prepare a remediation plan.")
    s.handle("inject a gray failure")
    s.handle("continue")
    task = s.tasks[-1]
    assert task["failover"] is not None
    pre_gen = task["pre_failover_generation"]
    cur_gen = s.layer.fencing.current("remediation")
    assert pre_gen < cur_gen, "failover should have rotated the generation"

    msgs = s.handle("test the fence")
    assert len(msgs) == 1 and msgs[0]["kind"] == "fence_test", msgs
    m = msgs[0]
    assert m["rejected"] is True, "stale write must be rejected"
    assert m["attempted_generation"] == pre_gen
    assert m["current_generation"] == cur_gen
    assert "Simulated fault" in m["honest_label"]
    kinds = [e["kind"] for e in s.layer.ledger.timeline]
    assert "stale_write_rejected" in kinds, "rejection must hit the ledger"
    print("PASS fence: stale write from fenced primary rejected at the store")


def test_fence_test_needs_failover_first():
    s = SimSession()
    s.handle("Check the checkout service error rate.")
    msgs = s.handle("test the fence")
    assert "failover" in msgs[0]["text"].lower()
    print("PASS fence: no failover yet -> honest nudge, not a fake test")


def test_faults_labeled_honestly():
    s = SimSession()
    s.handle("Investigate incident INC-1043 and prepare a remediation plan.")
    msgs = s.handle("inject a gray failure")
    text = msgs[0]["text"]
    assert "Simulated fault (deterministic, preconfigured)" in text, text
    assert "never restart the database" in text, "invariant must be named"
    msgs = s.handle("continue")
    contrast = next(m for m in msgs if m["kind"] == "contrast")
    assert "Simulated fault" in contrast["fault_label"]
    assert any("never restart the database" in i
               for i in contrast["invariants"])
    print("PASS honesty: faults labeled simulated; invariants named, not implied")


def test_nlu_fence_test_intent():
    assert parse_request("test the fence")["intent"] == "fence_test"
    assert parse_request("stale write")["intent"] == "fence_test"
    print("PASS nlu: fence_test intent")


def test_incident_timeline_includes_failover_evidence():
    # The per-incident timeline must not silently drop the recovery:
    # standby_activated carries resumed_from_incident (not incident), so
    # the view matches on both.
    from continuity.mcp_server import LedgerView
    s = SimSession()
    s.handle("Investigate incident INC-1043 and prepare a remediation plan.")
    s.handle("inject a gray failure")
    s.handle("continue")
    view = LedgerView(s.layer.ledger.export_dict())
    kinds = [e["kind"]
             for e in view.get_incident("INC-1043")["timeline_entries"]]
    assert "standby_activated" in kinds, f"failover evidence dropped: {kinds}"
    print("PASS incident view: standby_activated present in incident timeline")


def test_chat_ablation_message():
    s = SimSession()
    msgs = s.handle("run the ablation")
    assert len(msgs) == 1 and msgs[0]["kind"] == "ablation", msgs
    comp = msgs[0]["comparison"]
    assert len(comp["rows"]) == 5
    assert "python3 -m sim.ablation" in comp["scorecard"]["rerun"]
    print("PASS chat: 'run the ablation' returns the live comparison")


def test_ablation_silent_failure():
    baseline = run_baseline("silent", "INC-1043")
    protected = run_protected("silent", "INC-1043")
    assert protected["disposition"] == "replayed by standby"
    comp = compare(baseline, protected)
    assert comp["scorecard"]["buckets"]["recovered_via_standby"] == ["INC-1043"]
    print("PASS ablation: silent failure -> replayed by standby")


def test_no_banned_language_in_new_files():
    import re
    root = os.path.dirname(os.path.abspath(__file__))
    # New source modules only (this test file itself names the banned
    # phrase inside its own assertion pattern, so it is out of scope).
    files = [os.path.join(root, "sim", f)
             for f in ("__init__.py", "alexa_plus_sim.py", "ablation.py",
                       "scorecard.py")]
    for path in files:
        text = open(path, encoding="utf-8").read()
        assert "$20K" not in text and "$20k" not in text, f"banned in {path}"
        assert not re.search(r"\bAI assistant\b", text, re.IGNORECASE), \
            f"banned phrase in {path}"
    print("PASS copy: no $20K language; framed as a reliability layer")


def test_booking_utterance_runs_the_booking_flow():
    """The advertised airport-pickup request must map to the working
    booking scenario -- never a generic help response. The chat narrates
    the protected run start to finish and ends with a journal-built
    receipt showing exactly one booking."""
    session = SimSession()
    msgs = session.handle(
        "Arrange an airport pickup for 6:30 tomorrow, add it to my "
        "calendar, and send my partner the details.")
    assert len(msgs) == 6, [m["kind"] for m in msgs]
    assert all(m["kind"] == "text" for m in msgs[:5])
    assert msgs[-1]["kind"] == "booking_receipt"
    # The narration names the honest fault and the journal check.
    joined = " ".join(m.get("text", "") for m in msgs)
    assert "Simulated fault" in joined, joined
    assert "operation journal" in joined, joined
    # The receipt is built from real records: one booking, reused.
    receipt = msgs[-1]["receipt"]
    ops = receipt["operations"]
    assert len(ops) == 3, ops
    ride = ops[0]
    assert ride["retry_decision"] == "reused", ride
    assert (ride["effect_receipt"] or {}).get("status") == "confirmed"
    print("PASS booking: advertised utterance -> narrated recovery + receipt")


def _dashboard_client():
    from dashboard.app import create_app
    try:
        app = create_app()
    except TypeError:
        import dashboard.app as appmod
        app = appmod.app
    app.config["TESTING"] = True
    return app.test_client()


def test_challenge_verdict_badges_render():
    client = _dashboard_client()
    html = client.post("/sim/api/challenge",
                       json={"scenario": "lost_ack"}).get_json()["html"]
    assert 'class="verdict ok"' in html, "lost_ack challenge must show a success verdict badge"
    assert "no duplicates" in html.lower(), html
    assert 'class="verdict warn"' in html, "lost_ack must flag the halted provider-state-loss variant"
    assert "human review" in html.lower(), html
    html_u = client.post("/sim/api/challenge",
                         json={"scenario": "unsafe"}).get_json()["html"]
    assert "blocked" in html_u.lower(), html_u
    html_s = client.post("/sim/api/challenge",
                         json={"scenario": "stale_write"}).get_json()["html"]
    assert "rejected" in html_s.lower(), html_s
    print("PASS ui: challenge verdict badges render for all three scenarios")


def test_receipt_halt_styling_on_human_review():
    from dashboard.app import _render_receipt
    halted_receipt = {
        "incident_id": "CHAL-TEST-1", "operations": [],
        "checkpoint": "n/a", "fencing": "n/a",
        "final_state": {"disposition": "halted",
                        "duplicates_prevented": 0,
                        "human_review_flags": ["provider state unverifiable"]},
    }
    html = _render_receipt(halted_receipt)
    assert 'class="receipt halt"' in html, "halted receipt must carry the halt style"
    assert "human review required" in html.lower(), html
    clean_receipt = dict(halted_receipt)
    clean_receipt["final_state"] = {"disposition": "completed clean",
                                    "duplicates_prevented": 1,
                                    "human_review_flags": []}
    html2 = _render_receipt(clean_receipt)
    assert 'class="receipt halt"' not in html2, "clean receipt must not look halted"
    print("PASS ui: receipt halt styling tracks human_review_flags honestly")


def test_sim_page_has_reset_and_light_theme():
    client = _dashboard_client()
    html = client.get("/sim").get_data(as_text=True)
    assert 'id="runBtn"' in html, "primary CTA must keep its id"
    assert "resetDemo()" in html, "reset flow must exist"
    assert "--bg: #f6f8fa" in html, "light theme tokens must be present"
    assert "Run the booking scenario" in html, html
    assert "simulated" in html.lower(), "simulated disclosure must be present"
    print("PASS ui: /sim page has reset, light theme, CTA, and simulated disclosure")


def test_taskcard_unified_single_action():
    client = _dashboard_client()
    html = client.get("/sim").get_data(as_text=True)
    # One primary action: the Run button drives the challenge flow
    assert html.count('id="runBtn"') == 1, "exactly one primary Run button"
    assert "runChallenge('lost_ack','scenario-out')" in html, "Run must use the existing challenge flow"
    # Technical table collapsed into progressive disclosure, not a wall of text
    assert '<details class="how">' in html, "meta table must be in a disclosure"
    assert "How this demo works" in html
    # The natural-language request stays the hero
    assert "Arrange an airport pickup for 6:30 tomorrow" in html
    print("PASS ui: task card unified — one action, request as hero, details disclosed")


TESTS = [test_nlu_conversational_prompt_shape,
         test_nlu_all_task_specs,
         test_nlu_control_intents,
         test_session_stays_offline_with_key_set,
         test_full_beat_gray_failure,
         test_silent_failure_recovered_via_standby,
         test_what_happened_three_buckets,
         test_checkpoint_incident_scoped,
         test_ablation_baseline_executes_what_layer_blocks,
         test_fence_test_rejects_stale_write,
         test_fence_test_needs_failover_first,
         test_faults_labeled_honestly,
         test_nlu_fence_test_intent,
         test_chat_ablation_message,
         test_ablation_silent_failure,
         test_incident_timeline_includes_failover_evidence,
         test_no_banned_language_in_new_files,
         test_booking_utterance_runs_the_booking_flow,
         test_challenge_verdict_badges_render,
         test_receipt_halt_styling_on_human_review,
         test_sim_page_has_reset_and_light_theme,
         test_taskcard_unified_single_action]


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
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} sim tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

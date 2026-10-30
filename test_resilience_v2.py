"""Resilience regression tests: fencing, journal crash recovery,
concurrent writers, parallel scenario runs, shared-layer concurrency.

Zero network. Every test asserts behavior the current code already
provides -- these lock it in against regressions, they do not demand
new behavior.
"""
import os
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from continuity.fencing import FencingAuthority, StaleEpochError
from continuity.journal import OperationJournal
from examples.independent_support import _make_layer

TESTS = []


def test_fencing_stale_generation_rejected():
    auth = FencingAuthority()
    gen1 = auth.rotate("remediation")
    assert auth.current("remediation") == gen1
    auth.check("remediation", gen1)  # current generation passes
    gen2 = auth.rotate("remediation")
    assert gen2 == gen1 + 1
    assert auth.current("remediation") == gen2
    try:
        auth.check("remediation", gen1)
    except StaleEpochError:
        pass
    else:
        raise AssertionError("stale generation was not rejected")
    auth.check("remediation", gen2)
    print("PASS resilience: stale fencing generation rejected, current "
          "accepted")


def _crash_child_script() -> str:
    return (
        "import os, sys\n"
        "sys.path.insert(0, os.environ['REPO'])\n"
        "from continuity.journal import OperationJournal\n"
        "j = OperationJournal(os.environ['JPATH'])\n"
        "j.record_intent('op-1', 'key-1', service='payments-api',\n"
        "                op='book', target='ride',\n"
        "                params={'ride': 'R-1'})\n"
        "j.record_commit('op-1', {'receipt': 'RCPT-1'})\n"
        "j.record_intent('op-2', 'key-2', service='payments-api',\n"
        "                op='book', target='ride',\n"
        "                params={'ride': 'R-2'})\n"
        "os._exit(0)  # crash: no close(), no cleanup\n"
    )


def test_journal_survives_process_crash():
    with tempfile.TemporaryDirectory() as tmp:
        child = os.path.join(tmp, "crash_child.py")
        with open(child, "w") as fh:
            fh.write(_crash_child_script())
        db = os.path.join(tmp, "journal.sqlite")
        env = dict(os.environ, REPO=os.path.dirname(
            os.path.abspath(__file__)), JPATH=db)
        proc = subprocess.run([sys.executable, child], env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        j = OperationJournal(db)
        try:
            committed = j.get("op-1")
            assert committed is not None
            assert committed["status"] == "committed"
            assert committed["receipt"] == {"receipt": "RCPT-1"}
            pending = j.get("op-2")
            assert pending is not None
            assert pending["status"] == "pending"
        finally:
            j.close()
    print("PASS resilience: journal survives a process crash without "
          "close() -- committed receipt and pending intent both present")


def test_journal_concurrent_writers_lose_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "journal.sqlite")
        journal = OperationJournal(db)
        errors = []

        def write_batch(worker: int):
            try:
                for i in range(10):
                    journal.record_intent(
                        f"op-{worker}-{i}", f"key-{worker}-{i}",
                        service="payments-api", op="book", target="ride",
                        params={"ride": f"R-{worker}-{i}"})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write_batch, args=(w,))
                   for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            assert errors == [], errors
            records = journal.all_records()
            assert len(records) == 80, len(records)
        finally:
            journal.close()
    print("PASS resilience: 8 concurrent journal writers, 80/80 intents "
          "recorded, none lost")


def test_parallel_challenge_runs_stay_independent():
    from sim.ablation import run_challenge
    results = []
    errors = []

    def run_one():
        try:
            results.append(run_challenge("lost_ack"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run_one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    assert len(results) == 4
    incident_ids = {r["incident_id"] for r in results}
    assert len(incident_ids) == 4, incident_ids
    for r in results:
        assert r["receipt"]["final_state"]["disposition"] == \
            "completed clean", r["receipt"]["final_state"]
    print("PASS resilience: 4 parallel lost-ack challenges, distinct "
          "incidents, all completed clean, no cross-talk")


def test_shared_layer_concurrent_invokes():
    layer, shims = _make_layer("normal")
    outputs = {}
    errors = []

    def invoke_one(n: int):
        try:
            ticket = {"id": f"T-{n}", "category": "payments",
                      "urgent": True}
            ctx = {"incident": {"id": f"TKT-T-{n}"}, "ticket": ticket}
            res = layer.invoke("support-triage", ctx, shims["triage"])
            outputs[n] = res["output"]
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=invoke_one, args=(n,))
               for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    assert len(outputs) == 4
    for out in outputs.values():
        assert out == {"severity": "P1", "category": "payments"}, out
    print("PASS resilience: shared layer, 4 concurrent triage invokes, "
          "all outputs correct")


TESTS = [test_fencing_stale_generation_rejected,
         test_journal_survives_process_crash,
         test_journal_concurrent_writers_lose_nothing,
         test_parallel_challenge_runs_stay_independent,
         test_shared_layer_concurrent_invokes]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} resilience tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

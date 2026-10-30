"""Robust-version tests for the Agent Continuity Layer.

Extends the brutal proof (test_continuity.py, untouched): config, thread
safety, checkpoint schema versioning, forensic JSON export, dashboard
endpoints, and the Nemotron client's retry/circuit-breaker fallback --
all with zero network calls.

Run:  python3 test_robust.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_continuity import make_world, process_incident, incident

from continuity.adapter import ContinuityLayer
from continuity.checkpoint import (SCHEMA_VERSION, ContinuityPackage,
                                   UnknownSchemaVersion)
from continuity.config import DEFAULTS, get, load_config
from continuity.diagnostician import NemotronDiagnostician, StubDiagnostician
from continuity import metrics as m


# ---------------------------------------------------------------------------
# 1. Config: defaults reproduce current behavior; YAML overrides apply
# ---------------------------------------------------------------------------

def test_config_defaults_and_override():
    cfg = load_config()  # no continuity.yaml in repo root -> defaults
    assert get(cfg, "hysteresis", "anomaly_threshold") == 2
    assert get(cfg, "hysteresis", "clean_streak_to_heal") == 2
    assert get(cfg, "nemotron", "model") == "nvidia/Nemotron-3.5-Super-VL"
    assert get(cfg, "nemotron", "timeout_s") == 20
    assert get(cfg, "nemotron", "retries") == 2

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("hysteresis:\n  anomaly_threshold: 5\n"
                 "nemotron:\n  model: nvidia/Custom-1\n")
        path = fh.name
    try:
        over = load_config(path)
        assert get(over, "hysteresis", "anomaly_threshold") == 5
        assert get(over, "hysteresis", "clean_streak_to_heal") == 2  # merged
        assert get(over, "nemotron", "model") == "nvidia/Custom-1"
        assert get(over, "nemotron", "timeout_s") == 20  # untouched default
    finally:
        os.unlink(path)

    # The sentinel honors config thresholds.
    layer = ContinuityLayer(config=over)
    assert layer.sentinel.anomaly_threshold == 5
    assert layer.sentinel.clean_streak_to_heal == 2
    print("PASS config: defaults reproduce behavior; YAML overrides merge")


# ---------------------------------------------------------------------------
# 2. Thread safety: 4 threads x 25 invocations on one shared layer
# ---------------------------------------------------------------------------

def test_threaded_smoke_no_lost_updates_no_duplicates():
    layer, store, primaries = make_world()
    errors = []
    per_thread = 25
    n_threads = 4

    def worker(tid: int):
        try:
            for n in range(per_thread):
                inc = incident(f"INC-T{tid}-{n:03d}")
                process_incident(layer, store, primaries, inc)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker errors: {errors[:2]}"
    # One remediation effect per incident; nothing lost, nothing doubled.
    assert len(store.applied) == n_threads * per_thread, len(store.applied)
    assert layer.ledger.duplicates_prevented == 0
    affected = layer.ledger.affected_work()
    assert len(affected) == n_threads * per_thread
    assert all(d == "completed clean" for d in affected.values())
    print("PASS threaded smoke: 4x25 invocations, no lost updates, "
          "no duplicate effects")


# ---------------------------------------------------------------------------
# 3. Checkpoint schema versioning: unknown versions rejected, fail closed
# ---------------------------------------------------------------------------

def test_checkpoint_unknown_version_rejected():
    pkg = ContinuityPackage(agent_id="remediation", role="remediation",
                            incident_id="INC-1", tick=3)
    assert pkg.schema_version == SCHEMA_VERSION
    pkg.check_version()  # current version: fine

    future = ContinuityPackage(agent_id="remediation", role="remediation",
                               incident_id="INC-1", tick=3, schema_version=999)
    try:
        future.check_version()
        raise AssertionError("unknown schema version was accepted")
    except UnknownSchemaVersion:
        pass

    # Standby refuses the package loudly; the adapter fails over fresh.
    from continuity.standby import StandbyController
    from agents.standbys import make_standby
    sb = StandbyController(make_standby("remediation"))
    try:
        sb.activate(future)
        raise AssertionError("standby accepted an unreadable package")
    except UnknownSchemaVersion:
        pass
    assert not sb.active and sb.package is None

    # End-to-end: failover with a poisoned checkpoint still takes over.
    layer, store, primaries = make_world()
    process_incident(layer, store, primaries, incident("INC-800"))
    bad = ContinuityPackage(agent_id="remediation", role="remediation",
                            incident_id="INC-801", tick=9, schema_version=999,
                            planned_action_ids=["INC-801:remediation:0:rollback:payments-api"])
    layer._checkpoints[("remediation", "INC-801")] = bad
    primaries["remediation"].backend.mode = "llm_down"
    ctx = process_incident(layer, store, primaries, incident("INC-801"))
    assert ctx["remediation"]["remediation"] is not None  # standby served
    kinds = [e["kind"] for e in layer.ledger.timeline]
    assert "continuity_package_rejected" in kinds
    print("PASS checkpoint versioning: unknown schema rejected, "
          "failover continues fresh")


# ---------------------------------------------------------------------------
# 4. Ledger JSON export: schema-valid forensic report
# ---------------------------------------------------------------------------

def test_ledger_export_json():
    layer, store, primaries = make_world(diagnostician=StubDiagnostician())
    process_incident(layer, store, primaries, incident("INC-1042"))
    primaries["remediation"].backend.mode = "llm_down"
    process_incident(layer, store, primaries, incident("INC-1043"))
    primaries["remediation"].alive = True
    primaries["remediation"].backend.mode = "healthy"
    assert layer.attempt_rejoin("remediation") is True

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "forensic.json")
        assert layer.ledger.export_json(path) == path
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)

    assert report["schema"] == "agent-continuity/forensic-report@1"
    assert len(report["failure_windows"]) == 1
    w = report["failure_windows"][0]
    assert w["first_anomaly_tick"] <= w["failover_tick"] <= w["end_tick"]
    assert set(report["affected_work"]) == {"INC-1042", "INC-1043"}
    assert report["affected_work"]["INC-1043"] == "replayed by standby"
    metrics = report["metrics"]
    assert metrics["tsfd_ticks"] is not None
    assert metrics["trv_ticks"] is not None and metrics["trv_ticks"] >= 0
    assert 0.0 <= metrics["afr"] <= 1.0
    ticks = [e["tick"] for e in report["timeline"]]
    assert ticks == sorted(ticks)
    assert any("nemotron-diagnosis" in n["note"] or "stub-diagnosis" in n["note"]
               for n in report["diagnostician_notes"])
    assert isinstance(report["duplicates_prevented"], int)
    print("PASS ledger export: valid forensic JSON with windows, "
          "affected-work map, TSFD/TRV/AFR")


def test_metrics_definitions():
    layer, store, primaries = make_world()
    assert m.tsfd(layer.ledger) is None
    assert m.trv(layer.ledger) is None
    assert m.afr(layer.ledger) is None
    process_incident(layer, store, primaries, incident("INC-1042"))
    primaries["remediation"].backend.mode = "llm_down"
    process_incident(layer, store, primaries, incident("INC-1043"))
    assert m.tsfd(layer.ledger) == layer.ledger.tsfd()
    assert m.tsfd(layer.ledger) >= 0
    assert m.trv(layer.ledger) is None  # window not closed yet
    assert m.afr(layer.ledger) == 1.0   # both incidents recovered
    print("PASS metrics: TSFD/TRV/AFR definitions hold")


# ---------------------------------------------------------------------------
# 5. Dashboard: endpoints serve 200 + JSON on an ephemeral port
# ---------------------------------------------------------------------------

def test_dashboard_endpoints():
    try:
        from werkzeug.serving import make_server
        from dashboard.app import create_app
    except ImportError:
        print("SKIP dashboard: flask/werkzeug not installed "
              "(pip install -r requirements.txt); dashboard is optional")
        return

    layer, store, primaries = make_world()
    process_incident(layer, store, primaries, incident("INC-1042"))
    primaries["remediation"].backend.mode = "gray"
    process_incident(layer, store, primaries, incident("INC-1043"))

    app = create_app(layer=layer)
    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        import urllib.request
        def get(path):
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                assert r.status == 200, path
                return r.read().decode()
        html = get("/")
        assert "AGENT CONTINUITY LAYER" in html
        health = json.loads(get("/api/health"))
        assert health["roles"]["remediation"] == "FAILOVER"
        assert set(health["roles"]) == {"triage", "diagnosis", "remediation",
                                        "escalation"}
        tl = json.loads(get("/api/timeline?limit=10"))
        assert len(tl["events"]) <= 10 and all(
            {"tick", "kind", "summary"} <= set(e) for e in tl["events"])
        aff = json.loads(get("/api/affected"))
        assert aff["affected_work"]["INC-1043"] in ("replayed by standby",
                                                   "human review")
        money = json.loads(get("/api/money"))
        assert money["metrics"]["tsfd_ticks"] is not None
        assert money["window"]["failover_tick"] is not None
        rep = json.loads(get("/api/report"))
        assert rep["schema"] == "agent-continuity/forensic-report@1"
    finally:
        server.shutdown()
    print("PASS dashboard: all endpoints 200 with JSON on ephemeral port")


# ---------------------------------------------------------------------------
# 6. Nemotron client: retries + circuit breaker degrade silently, no network
# ---------------------------------------------------------------------------

def test_nemotron_retry_then_success():
    calls = []

    def flaky(body):
        calls.append(body["model"])
        if len(calls) < 3:
            raise ConnectionError("transient")
        return "The role lost its model connection."

    d = NemotronDiagnostician(api_key="k-never-sent", transport=flaky,
                              backoff_base_s=0, retries=2)
    note = d.diagnose("remediation", {"contract": False}, ["v"])
    assert "nemotron-diagnosis(remediation)" in note
    assert "nvidia/Nemotron-3.5-Super-VL" in note
    assert len(calls) == 3 and all(c == "nvidia/Nemotron-3.5-Super-VL" for c in calls)
    assert not d.circuit_open
    print("PASS nemotron: 2 retries then success, no exception")


def test_nemotron_circuit_breaker_fallback():
    def dead(body):
        raise TimeoutError("hang")

    d = NemotronDiagnostician(api_key="k-never-sent", transport=dead,
                              backoff_base_s=0, retries=0,
                              circuit_breaker_failures=3,
                              circuit_breaker_open_s=60)
    for _ in range(3):
        note = d.diagnose("remediation", {"contract": False}, ["v"])
        assert "deterministic verdict stands" in note  # never raises
    assert d.circuit_open
    # While open the transport is not even called: silent stub fallback.
    calls = []

    def fail_once(body):
        calls.append(body)
        raise ConnectionError("first call fails")

    d2 = NemotronDiagnostician(api_key="k-never-sent", transport=fail_once,
                               backoff_base_s=0, retries=0,
                               circuit_breaker_failures=1,
                               circuit_breaker_open_s=60)
    d2.diagnose("r", {}, [])   # 1 failure -> breaker opens
    assert d2.circuit_open
    note = d2.diagnose("r", {"contract": False}, [])
    assert "stub-diagnosis" in note and len(calls) == 1
    print("PASS nemotron: circuit opens after 3 failures, silent stub fallback")


def test_nemotron_never_raises_into_sentinel():
    def explosive(body):
        raise RuntimeError("catastrophic transport bug")

    d = NemotronDiagnostician(api_key="k", transport=explosive, backoff_base_s=0,
                              retries=0, circuit_breaker_failures=100)
    # Even a bug INSIDE the client must not propagate: sentinel stays safe.
    note = d.diagnose("remediation", {"contract": False}, [])
    assert "deterministic verdict stands" in note

    # And the sentinel reaches identical verdicts with this client attached.
    def run(diag):
        layer, store, primaries = make_world(diagnostician=diag)
        process_incident(layer, store, primaries, incident("INC-1042"))
        primaries["remediation"].backend.mode = "llm_down"
        process_incident(layer, store, primaries, incident("INC-1043"))
        return layer.ledger.windows[0]["failover_tick"]

    assert run(None) == run(d)
    print("PASS nemotron: never raises; sentinel verdicts identical on/off")


# ---------------------------------------------------------------------------

TESTS = [
    test_config_defaults_and_override,
    test_threaded_smoke_no_lost_updates_no_duplicates,
    test_checkpoint_unknown_version_rejected,
    test_ledger_export_json,
    test_metrics_definitions,
    test_dashboard_endpoints,
    test_nemotron_retry_then_success,
    test_nemotron_circuit_breaker_fallback,
    test_nemotron_never_raises_into_sentinel,
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

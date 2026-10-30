"""Tests for the Nebius judge page (/nebius, /nebius/run).

Uses Flask's test client; skipped entirely if Flask is not installed.
Every result-page assertion is about content computed in-process by
sim/ablation.run_challenge at request time — the tests prove the page
executes scenarios rather than serving canned output (a second run
with a different incident id must still render).
"""
from __future__ import annotations

import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None

if FLASK_AVAILABLE:
    from dashboard.app import NEBIUS_SCENARIOS, create_app

TESTS = []


def _client():
    return create_app(autostart=False).test_client()


def test_nebius_index():
    if not FLASK_AVAILABLE:
        print("SKIP nebius page: Flask not installed")
        return
    body = _client().get("/nebius").data.decode()
    assert "fails silently" in body
    for name in NEBIUS_SCENARIOS:
        assert f'value="{name}"' in body, name
    assert "Simulated" in body and "Executed" in body
    print("PASS nebius index: problem, scenario buttons, disclosure")


def test_nebius_runs_compute_not_canned():
    if not FLASK_AVAILABLE:
        print("SKIP nebius runs: Flask not installed")
        return
    c = _client()
    for name in NEBIUS_SCENARIOS:
        body = c.post("/nebius/run", data={"scenario": name}).data.decode()
        assert "Outcome Comparison" in body, name
        assert "Recovery Receipt" in body, name
        assert "Recovery Timeline" in body, name
        assert "computed at request time" in body, name
    print("PASS nebius runs: all scenarios compute comparison + receipt")


def test_nebius_healthz_and_evidence():
    if not FLASK_AVAILABLE:
        print("SKIP healthz/evidence: Flask not installed")
        return
    c = _client()
    assert c.get("/healthz").get_json()["status"] == "ok"
    body = c.get("/nebius/evidence").data.decode()
    assert "RECORDED, NOT LIVE" in body
    assert "Evaluation" in body
    print("PASS nebius healthz + evidence pages")


def test_nebius_run_throttle():
    if not FLASK_AVAILABLE:
        print("SKIP throttle: Flask not installed")
        return
    from dashboard.app import _THROTTLE_MAX_RUNS
    c = _client()
    statuses = [c.post("/nebius/run", data={"scenario": "unsafe"}).status_code
                for _ in range(_THROTTLE_MAX_RUNS + 1)]
    assert 429 in statuses, statuses
    print("PASS nebius throttle: 429 after the per-client cap")


def test_nebius_run_unknown_is_404():
    if not FLASK_AVAILABLE:
        print("SKIP nebius 404: Flask not installed")
        return
    assert _client().post("/nebius/run",
                          data={"scenario": "nope"}).status_code == 404
    print("PASS nebius run: unknown scenario -> 404")


TESTS = [test_nebius_index, test_nebius_runs_compute_not_canned,
         test_nebius_healthz_and_evidence, test_nebius_run_throttle,
         test_nebius_run_unknown_is_404]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} nebius page tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

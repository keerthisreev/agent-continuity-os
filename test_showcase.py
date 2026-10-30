"""Tests for the dashboard scenario showcase.

Uses Flask's test client; skipped entirely if Flask is not installed.
The gallery must list every scenario, each scenario must run to its
expected verdict, and the result page must show the verdict, the
affected-work map, and the forensic narrative side by side.
"""
from __future__ import annotations
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None

if FLASK_AVAILABLE:
    from dashboard.app import create_app, SHOWCASE_SCENARIOS


def _client():
    # No live replay thread: the gallery runs scenarios on demand.
    return create_app(autostart=False).test_client()


def test_showcase_index_lists_scenarios():
    if not FLASK_AVAILABLE:
        print("SKIP showcase: Flask not installed")
        return
    body = _client().get("/showcase").data.decode()
    for name, spec in SHOWCASE_SCENARIOS.items():
        assert f"/showcase/{name}" in body, f"missing link for {name}"
        assert spec["title"] in body, f"missing title for {name}"
    print(f"PASS showcase index: {len(SHOWCASE_SCENARIOS)} scenarios listed")


def test_showcase_gray_verdict():
    if not FLASK_AVAILABLE:
        print("SKIP showcase: Flask not installed")
        return
    body = _client().get("/showcase/gray").data.decode()
    assert "FAILOVER" in body
    assert "INC-1043" in body and "replayed by standby" in body
    assert "stub-narrative" in body
    print("PASS showcase gray: FAILOVER, affected-work map, narrative")


def test_showcase_healthy_no_failover():
    if not FLASK_AVAILABLE:
        print("SKIP showcase: Flask not installed")
        return
    body = _client().get("/showcase/healthy").data.decode()
    assert "HEALTHY" in body
    assert "FAILOVER" not in body
    assert "completed clean" in body
    print("PASS showcase healthy: no false failover")


def test_showcase_semantic_human_review():
    if not FLASK_AVAILABLE:
        print("SKIP showcase: Flask not installed")
        return
    body = _client().get("/showcase/semantic").data.decode()
    assert "FAILOVER" in body and "INC-1043" in body
    print("PASS showcase semantic: gray semantic caught and shown")


def test_showcase_unknown_is_404():
    if not FLASK_AVAILABLE:
        print("SKIP showcase: Flask not installed")
        return
    assert _client().get("/showcase/nope").status_code == 404
    print("PASS showcase 404: unknown scenario")


def test_showcase_live_view_still_works():
    if not FLASK_AVAILABLE:
        print("SKIP showcase: Flask not installed")
        return
    body = _client().get("/").data.decode()
    assert "AGENT CONTINUITY LAYER" in body
    assert "Role health" in body and "Affected work" in body
    assert "$20K" not in body and "20K screen" not in body
    print("PASS showcase: live view intact, no $20K panel")


TESTS = [
    test_showcase_index_lists_scenarios,
    test_showcase_gray_verdict,
    test_showcase_healthy_no_failover,
    test_showcase_semantic_human_review,
    test_showcase_unknown_is_404,
    test_showcase_live_view_still_works,
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

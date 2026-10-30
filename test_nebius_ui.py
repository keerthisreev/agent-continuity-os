"""UI regression + interaction tests for the Mission Control redesign.

Every assertion is against the Flask test client rendering the real
routes with the real scenario runner. No live model calls: the demo
runs offline, and recorded content must stay labeled as recorded.
"""
from __future__ import annotations

import html
import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FLASK_AVAILABLE = importlib.util.find_spec("flask") is not None

if FLASK_AVAILABLE:
    from dashboard.app import NEBIUS_SCENARIOS, create_app
    from sim.ablation import run_challenge

SECRET_PATTERNS = [
    re.compile(r"Bearer [A-Za-z0-9._-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{8,}"),
    re.compile(r"NEBIUS_API_KEY\s*=\s*['\"][^'\"]{8,}"),
]

NAV_LABELS = ["Overview", "Failure Lab", "Recovery Timeline",
              "Evidence", "Evaluation"]


def _client():
    if not FLASK_AVAILABLE:
        print("SKIP nebius ui: Flask not installed")
        return None
    return create_app(autostart=False).test_client()


def _page(client, method, path, **kwargs):
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 200, (path, resp.status_code)
    return html.unescape(resp.data.decode())


def test_index_nav_status_and_no_raw_logs():
    c = _client()
    if c is None:
        return
    body = _page(c, "get", "/nebius")
    for label in NAV_LABELS:
        assert label in body, label
    assert "viewport" in body
    assert "mission.css" in body and "mission.js" in body
    # Real status, not hardcoded success.
    assert "Continuity engine" in body
    assert "Operational" in body or "Degraded" in body
    assert "Offline demo" in body
    assert "Recorded runs" in body
    assert "4 scenarios" in body
    # Primary screen: no terminal logs, no raw JSON blobs.
    assert "<pre" not in body
    assert "DEMO MODE" in body
    # All four Failure Lab scenarios are choosable.
    for name in ("lost_ack", "unsafe", "stale_write", "silent"):
        assert f'name="scenario" value="{name}"' in body, name
    print("PASS nebius ui: index nav, real status, no raw logs, 4 scenarios")


def test_all_scenarios_match_backend_evidence():
    c = _client()
    if c is None:
        return
    expected_badges = {
        "lost_ack": ("tag-green", "Reconciled"),
        "unsafe": ("tag-amber", "human review"),
        "stale_write": ("tag-amber", "human review"),
        "silent": ("tag-green", "replayed by standby"),
    }
    for name in NEBIUS_SCENARIOS:
        result = run_challenge(name)
        body = _page(c, "post", "/nebius/run", data={"scenario": name})
        # The page generates its own incident id; mask it so
        # incident-embedded strings (e.g. stale action ids) compare.
        page_incident = re.search(
            r"Incident <strong>([^<]+)</strong>", body)
        assert page_incident, name
        assert page_incident.group(1).startswith("CHAL-"), name
        body = body.replace(page_incident.group(1),
                            result["incident_id"])
        for anchor in ("#timeline", "#comparison", "#nemotron", "#receipt"):
            assert anchor in body, (name, anchor)
        for stage in result["stages"]:
            assert stage["name"] in body, (name, stage["name"])
            assert stage["detail"] in body, (name, stage["name"])
        for row in result["comparison"]["rows"]:
            for key in ("baseline", "idempotency_only", "protected"):
                value = row.get(key)
                if value:
                    assert value in body, (name, row["aspect"], key)
        for op in result["receipt"]["operations"]:
            assert op["label"] in body, (name, op["label"])
        badge_class, badge_text = expected_badges[name]
        assert badge_class in body and badge_text in body, name
        assert "Copy receipt" in body
        assert "Technical receipt" in body
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(body), (name, pattern.pattern)
    print("PASS nebius ui: all 4 scenarios match backend evidence")


def test_no_false_recovery_claim():
    c = _client()
    if c is None:
        return
    for name in ("unsafe", "stale_write"):
        body = _page(c, "post", "/nebius/run", data={"scenario": name})
        lowered = body.lower()
        assert "recovered successfully" not in lowered, name
        assert "recovery successful" not in lowered, name
        assert "human review" in lowered, name
    print("PASS nebius ui: halted runs never claim successful recovery")


def test_evidence_recorded_and_evaluation_computed():
    c = _client()
    if c is None:
        return
    body = _page(c, "get", "/nebius/evidence")
    assert "RECORDED, NOT LIVE" in body
    assert "nothing here is live execution" in body.lower()
    for row_label in ("Lost ack (1 pickup)", "Lost ack + provider amnesia",
                      "Unsafe action (db restart)", "Stale write"):
        assert row_label in body, row_label
    assert "parity" in body.lower()
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(body), pattern.pattern
    print("PASS nebius ui: evidence labeled recorded, evaluation computed")


def test_static_assets_responsive():
    c = _client()
    if c is None:
        return
    css = c.get("/static/nebius/mission.css")
    assert css.status_code == 200
    assert "@media" in css.data.decode()
    js = c.get("/static/nebius/mission.js")
    assert js.status_code == 200
    assert "Running" in js.data.decode() and "Copy receipt" in js.data.decode()
    print("PASS nebius ui: static assets serve, responsive CSS present")


TESTS = [
    test_index_nav_status_and_no_raw_logs,
    test_all_scenarios_match_backend_evidence,
    test_no_false_recovery_claim,
    test_evidence_recorded_and_evaluation_computed,
    test_static_assets_responsive,
]


def main() -> int:
    failures = 0
    for fn in TESTS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    passed = len(TESTS) - failures
    print(f"\n{passed}/{len(TESTS)} nebius ui tests passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

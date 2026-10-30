"""Tests for Phase 2.4 (canary schema validation) and 2.6 (usage guardrail).

All transports mocked: zero network calls. Plain asserts, prints PASS.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from continuity.canary import (NemotronCanaryGenerator, StaticCanaryGenerator,
                               _validate_scenario)
from continuity.nemotron import NemotronClient
from continuity.usage import BudgetExceeded, UsageTracker

TESTS = []


def test_validate_accepts_well_formed():
    s = {"name": "db-latency", "incident": {"service": "orders-db",
         "symptom": "p99 8s", "severity": "P1"}}
    out = _validate_scenario(s)
    assert out and out["incident"]["service"] == "orders-db"
    assert out["name"] == "db-latency"
    print("PASS validate: well-formed scenario accepted")


def test_validate_rejects_malformed():
    bad = [
        "not-a-dict",
        {"incident": "not-a-dict"},
        {"incident": {"service": "", "symptom": "x", "severity": "P1"}},
        {"incident": {"service": "db", "symptom": "x"}},  # missing severity
        {"incident": {"service": "db", "symptom": "x", "severity": "P1"},
         "diagnosis": "not-a-dict"},
        {"incident": {"service": "db", "symptom": "x", "severity": "P1"},
         "name": 42},
    ]
    for s in bad:
        assert _validate_scenario(s) is None, s
    print("PASS validate: 6 malformed shapes rejected")


def test_generator_drops_invalid_keeps_valid():
    scenarios = [
        {"name": "good-one", "incident": {"service": "payments-api",
         "symptom": "error rate > 5%", "severity": "P1"}},
        {"incident": {"service": "db"}},  # invalid: missing fields
        {"name": "good-two", "incident": {"service": "orders-db",
         "symptom": "p99 8s", "severity": "P2"}},
    ]
    def transport(body):
        return json.dumps(scenarios)
    gen = NemotronCanaryGenerator(
        client=NemotronClient(api_key="test-key", transport=transport))
    tasks = gen.generate("remediation")
    names = [t.name for t in tasks]
    assert names == ["good-one", "good-two"], names
    assert all(t.oracle is not None for t in tasks)
    print("PASS generator: invalid scenario dropped, valid kept, oracles intact")


def test_generator_falls_back_when_all_invalid():
    def transport(body):
        return json.dumps([{"incident": {"service": "db"}}])
    gen = NemotronCanaryGenerator(
        client=NemotronClient(api_key="test-key", transport=transport))
    tasks = gen.generate("remediation")
    assert len(tasks) == 1 and tasks[0].name == "remediation-canary-1"
    print("PASS generator: all-invalid output -> silent static fallback")


def test_usage_cost_math():
    t = UsageTracker()
    t.record({"prompt_tokens": 24, "completion_tokens": 49})
    assert t.total_tokens == 73
    # 24 * 0.30 + 49 * 0.90 per 1M = $0.0000513
    assert abs(t.cost_usd - 0.0000513) < 1e-9, t.cost_usd
    print(f"PASS usage: 73 tokens -> ${t.cost_usd:.7f} (matches live smoke)")


def test_budget_blocks_before_spend():
    t = UsageTracker(budget_usd=0.00005)
    t.record({"prompt_tokens": 24, "completion_tokens": 49})  # $0.0000513
    try:
        t.check()
    except BudgetExceeded:
        print("PASS budget: spend past $1-test-budget equivalent refused")
        return
    raise AssertionError("budget check should have raised")


def test_default_budget_is_one_dollar():
    t = UsageTracker()
    assert t.budget_usd == 1.0
    t2 = UsageTracker(budget_usd=25.0)
    assert t2.budget_usd == 25.0
    print("PASS budget: default $1, configurable (never assumes $25 cap)")


def test_client_records_reported_usage():
    client = NemotronClient(api_key="test-key",
                            transport=lambda body: "hello")
    # Mock transports report no usage: tracker stays at zero, no crash.
    client.chat([{"role": "user", "content": "hi"}])
    assert client.usage.calls == 0 and client.last_usage is None
    # A transport that reports usage (like the real one) is recorded.
    holder: dict = {}
    def reporting_transport(body):
        holder["client"]._last_usage = {"prompt_tokens": 100,
                                        "completion_tokens": 50,
                                        "total_tokens": 150}
        return "hello"
    client2 = NemotronClient(api_key="test-key",
                             transport=reporting_transport)
    holder["client"] = client2
    client2.chat([{"role": "user", "content": "hi"}])
    assert client2.usage.total_tokens == 150
    assert client2.usage.calls == 1
    print("PASS client: reported usage recorded; unreported stays zero")


TESTS = [test_validate_accepts_well_formed,
         test_validate_rejects_malformed,
         test_generator_drops_invalid_keeps_valid,
         test_generator_falls_back_when_all_invalid,
         test_usage_cost_math,
         test_budget_blocks_before_spend,
         test_default_budget_is_one_dollar,
         test_client_records_reported_usage]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - test harness
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} usage/validation tests passed")
    return 1 if failed else 0


def test_persistence_round_trips():
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "usage.json")
    t = UsageTracker(persist_path=path)
    t.record({"prompt_tokens": 100, "completion_tokens": 50})
    t2 = UsageTracker(persist_path=path)
    assert t2.total_tokens == 150 and t2.calls == 1
    assert t2.summary()["persist_path"] == path
    print("PASS usage: file persistence round-trips across instances")


def test_concurrent_records_do_not_lose_tokens():
    import threading
    t = UsageTracker()
    def worker():
        for _ in range(200):
            t.record({"prompt_tokens": 1, "completion_tokens": 1})
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert t.total_tokens == 8 * 200 * 2, t.total_tokens
    assert t.calls == 8 * 200
    print("PASS usage: 8 threads x 200 records lose nothing")


TESTS.extend([test_persistence_round_trips,
              test_concurrent_records_do_not_lose_tokens])


if __name__ == "__main__":
    raise SystemExit(main())

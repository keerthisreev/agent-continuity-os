"""Tests for the adversarial benchmark (sim/adversarial_benchmark.py).

Offline by default: the Nemotron arm must report "skipped" without a
key, and no test here touches the network.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sim.adversarial_benchmark import (
    _BLIND_SPOTS,
    _CASES,
    run_benchmark,
    run_part1,
    run_part2,
)

TESTS = []

EXPECTED_GAPS = {
    "triage-severity-whitespace",
    "diagnosis-root-cause-whitespace",
    "remediation-restart-db-abbrev",
    "escalation-summary-whitespace",
    "escalation-both-whitespace",
}


def test_forty_cases_all_match_expected():
    part1 = run_part1()
    assert part1["cases"] == 40, part1["cases"]
    assert part1["mismatches"] == [], part1["mismatches"]
    assert part1["matched"] == 40
    print("PASS adversarial benchmark: 40/40 oracle cases match expected")


def test_gap_list_is_exact():
    """If oracle behavior changes, this fails on purpose: gaps must be
    re-reviewed deliberately, never silently absorbed."""
    part1 = run_part1()
    assert set(part1["gap_cases"]) == EXPECTED_GAPS, part1["gap_cases"]
    print("PASS adversarial benchmark: known-gap list exact "
          f"({len(EXPECTED_GAPS)} cases)")


def test_cases_cover_all_four_roles():
    roles = {}
    for case_id, role, *_ in _CASES:
        roles[role] = roles.get(role, 0) + 1
    assert roles == {"triage": 10, "diagnosis": 10, "remediation": 10,
                     "escalation": 10}, roles
    print("PASS adversarial benchmark: 10 cases per role, 4 roles")


def test_static_probe_detection_rates():
    part2 = run_part2(live=False)
    for role, det in part2["static_detection"].items():
        assert det["caught"] == ["always-degraded", "service-payments-api",
                                 "severity-p1"], (role, det)
        assert det["detection_rate"] == 0.75
    assert len(_BLIND_SPOTS) == 4
    print("PASS adversarial benchmark: static probe catches 3/4 blind "
          "agents per role (symptom-latency blind spot escapes it)")


def test_nemotron_arm_skips_offline():
    part2 = run_part2(live=False)
    assert part2["nemotron"]["status"] == "skipped"
    print("PASS adversarial benchmark: Nemotron arm skips offline, "
          "no network, no fabricated numbers")


def test_benchmark_is_deterministic():
    first = run_benchmark(live=False)
    second = run_benchmark(live=False)
    assert json.dumps(first, sort_keys=True) == json.dumps(second,
                                                           sort_keys=True)
    print("PASS adversarial benchmark: two runs byte-identical")


TESTS = [test_forty_cases_all_match_expected,
         test_gap_list_is_exact,
         test_cases_cover_all_four_roles,
         test_static_probe_detection_rates,
         test_nemotron_arm_skips_offline,
         test_benchmark_is_deterministic]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} adversarial benchmark "
          f"tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

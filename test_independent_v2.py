"""Second independence proof: a run(ticket) support pipeline, wrapped.

Zero network. Covers the three-role ticket pipeline in
examples/independent_support.py, whose agents live in
examples/independent_support_pipeline.py (no continuity imports).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from examples.independent_support import protected_support_run
from examples.independent_support_pipeline import (
    DiagnosisBot,
    EscalationBot,
    TriageBot,
)

TESTS = []

TICKET = {"id": "1043", "category": "payments", "urgent": True}


def test_pipeline_module_imports_no_continuity():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "examples",
                            "independent_support_pipeline.py")).read()
    assert "from continuity" not in src
    assert "import continuity" not in src
    print("PASS independent v2: pipeline module has no continuity import")


def test_agents_expose_run_not_handle():
    for bot in (TriageBot(), DiagnosisBot(), EscalationBot()):
        assert callable(getattr(bot, "run", None))
        assert not hasattr(bot, "handle")
    print("PASS independent v2: agents expose run(), not handle()")


def test_normal_run_chains_three_roles():
    out = protected_support_run(dict(TICKET))
    assert out["triage"] == {"severity": "P1", "category": "payments"}
    assert out["diagnosis"] == {"root_cause": "bad deployment v5.2",
                                "evidence": ["deploy log"]}
    assert out["escalation"]["ticket_id"] == "ESC-1043"
    assert "bad deployment v5.2" in out["escalation"]["summary"], \
        out["escalation"]
    print("PASS independent v2: normal run chains triage -> diagnosis "
          "-> escalation, summary enriched layer-side")


def test_degraded_diagnosis_fails_over_to_standby():
    out = protected_support_run(dict(TICKET), diagnosis_mode="degraded")
    assert out["diagnosis_active"] == "standby", out["diagnosis_active"]
    # The standby's output -- not the primary's empty dict, not a retry
    # of the broken primary.
    assert out["diagnosis"] == {"root_cause": "bad deployment v5.2",
                                "evidence": ["deploy log"]}
    assert "bad deployment v5.2" in out["escalation"]["summary"]
    print("PASS independent v2: degraded diagnosis fenced, standby output "
          "used, chain completes")


def test_standby_output_differs_from_degraded_primary():
    degraded = DiagnosisBot(mode="degraded").run(dict(TICKET))
    assert degraded == {}
    out = protected_support_run(dict(TICKET), diagnosis_mode="degraded")
    assert out["diagnosis"] != degraded
    assert out["diagnosis"]["root_cause"] == "bad deployment v5.2"
    print("PASS independent v2: final diagnosis provably came from the "
          "standby, not the degraded primary")


TESTS = [test_pipeline_module_imports_no_continuity,
         test_agents_expose_run_not_handle,
         test_normal_run_chains_three_roles,
         test_degraded_diagnosis_fails_over_to_standby,
         test_standby_output_differs_from_degraded_primary]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} independent v2 tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

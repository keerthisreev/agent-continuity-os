"""Determinism tests: model output must never decide a safety verdict.

Two locks, both offline:
1. The forensic narrative is post-hoc and advisory: two diagnosticians
   returning different prose produce identical failover/rejoin verdicts
   and identical ledger timelines except for the narrative entries.
2. Canary verdicts depend on the oracle + the agent's output, not on
   which generator wrote the scenario: model-written probes judged
   through the deterministic oracle give the same verdicts as the
   frozen static probe for the same agent behavior.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from continuity.canary import (
    CANARY_TASKS,
    CanaryRunner,
    NemotronCanaryGenerator,
)
from demo import run_scenario

TESTS = []


class _FakeDiagnostician:
    """Returns fixed prose; the narrative text differs per instance.

    diagnose() is identical for both instances (it is the in-run
    advisory note); only incident_narrative() differs, proving the
    post-hoc prose cannot move a verdict.
    """

    def __init__(self, text: str):
        self._text = text

    def diagnose(self, agent_id: str, health_vector: dict,
                 recent_violations) -> str:
        return "fake-diagnosis: advisory only, identical across instances"

    def incident_narrative(self, report) -> str:
        return self._text


def test_narrative_never_changes_verdicts():
    diag_a = _FakeDiagnostician("PROSE-A: the sky is blue")
    diag_b = _FakeDiagnostician("PROSE-B: the sky is green")
    layer_a, _, _, _, rejoined_a, narrative_a = run_scenario(
        "silent", diag_a, False)
    layer_b, _, _, _, rejoined_b, narrative_b = run_scenario(
        "silent", diag_b, False)
    assert narrative_a != narrative_b
    assert rejoined_a == rejoined_b == True  # noqa: E712
    timeline_a = [e for e in layer_a.ledger.timeline
                  if e["kind"] != "diagnostician_note"]
    timeline_b = [e for e in layer_b.ledger.timeline
                  if e["kind"] != "diagnostician_note"]
    assert timeline_a == timeline_b
    notes_a = [e for e in layer_a.ledger.timeline
               if e["kind"] == "diagnostician_note"
               and e["detail"].get("narrative") is True]
    notes_b = [e for e in layer_b.ledger.timeline
               if e["kind"] == "diagnostician_note"
               and e["detail"].get("narrative") is True]
    assert len(notes_a) == len(notes_b) == 1, (len(notes_a), len(notes_b))
    assert notes_a[0]["detail"]["note"] == "PROSE-A: the sky is blue"
    assert notes_b[0]["detail"]["note"] == "PROSE-B: the sky is green"
    print("PASS determinism: different narrative prose, identical "
          "verdicts and timelines (narrative entries excluded)")


class _FakeNemotronClient:
    """Pretends to be a model client; returns two valid scenarios."""

    available = True
    logger = None

    def chat(self, messages, temperature=0.7, max_tokens=800) -> str:
        return ('[{"name": "db-latency-spike", "incident": '
                '{"service": "orders-db", "symptom": "p99 latency 8s", '
                '"severity": "P1"}}, {"name": "cache-eviction-storm", '
                '"incident": {"service": "cache-01", "symptom": '
                '"eviction rate 40%", "severity": "P2"}}]')


class _StubAgent:
    def __init__(self, output):
        self._output = output

    def handle(self, ctx):
        import copy
        return copy.deepcopy(self._output)


def test_model_probes_judged_same_as_static():
    gen = NemotronCanaryGenerator(client=_FakeNemotronClient())
    assert gen.source == "nemotron"
    tasks = gen.generate("remediation")
    assert len(tasks) == 2, tasks
    healthy = {"remediation": "roll back to v5.1",
               "evidence": ["deploy log"],
               "actions": [{"kind": "rollback"}]}
    degraded = {}
    runner = CanaryRunner()
    static_task = CANARY_TASKS["remediation"]
    for stub_output, expected in ((healthy, True), (degraded, False)):
        static_verdict = runner.run(_StubAgent(stub_output),
                                    static_task).passed
        assert static_verdict == expected
        for task in tasks:
            verdict = runner.run(_StubAgent(stub_output), task).passed
            assert verdict == expected, (task.name, verdict, expected)
    print("PASS determinism: model-written probes, judged by the "
          "deterministic oracle, give the same verdicts as the static "
          "probe for the same agent behavior")


TESTS = [test_narrative_never_changes_verdicts,
         test_model_probes_judged_same_as_static]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} determinism tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

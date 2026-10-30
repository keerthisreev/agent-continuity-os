"""Generated vs static probe comparison, scored for real.

Runs two probe sets against two agent states and counts oracle verdicts:

  * static set: the frozen per-role probe (CANARY_TASKS)
  * generated set: Nemotron-written probes via the Nebius connector
    (skipped with a NOT MEASURED report when the connector is absent)

Agent states: healthy RemediationAgent vs gray RemediationAgent
(mode="gray": plausible-but-wrong output, the failure probes exist to
catch). A probe is USEFUL when it passes the healthy agent and fails
the degraded one; a probe that fails healthy is a false positive, and
one that passes degraded is a miss.

Also tracks latency and token usage for the generated set. No novelty
claim is made from this run alone: n is small (1 static probe, a
handful of generated ones), and the report says so.

Run:  python3 sim/compare_probes.py [--md]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.remediation import RemediationAgent
from continuity.canary import (CANARY_TASKS, CanaryRunner,
                               NemotronCanaryGenerator)
from continuity.nemotron import NemotronClient
from continuity.usage import UsageTracker

CONNECTOR_BIN = os.path.expanduser(
    "~/workspace/custom-connectors/custom.nebius-token-factory/bin")


def _connector_transport(tracker: UsageTracker):
    sys.path.insert(0, CONNECTOR_BIN)
    from nebius_chat import chat as connector_chat

    def transport(body: dict) -> str:
        tracker.check()
        result = connector_chat(
            body["messages"], temperature=body.get("temperature", 0.2),
            max_tokens=body.get("max_tokens", 300),
            model=body.get("model", "nvidia/Nemotron-3.5-Super-VL"))
        tracker.record(result.get("usage"))
        return result["choices"][0]["message"]["content"]

    return transport


def _score(runner: CanaryRunner, agent, tasks) -> dict:
    passed = failed = 0
    details = []
    for task in tasks:
        result = runner.run(agent, task)
        details.append((task.name, result.passed, result.reasons))
        if result.passed:
            passed += 1
        else:
            failed += 1
    return {"passed": passed, "failed": failed, "details": details}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", action="store_true")
    args = ap.parse_args(argv)

    runner = CanaryRunner()
    healthy = RemediationAgent(mode="healthy")
    degraded = RemediationAgent(mode="gray")

    static = [CANARY_TASKS["remediation"]]
    static_healthy = _score(runner, healthy, static)
    static_degraded = _score(runner, degraded, static)

    tracker = UsageTracker(budget_usd=1.0)
    generated, gen_latency, gen_error = [], 0.0, None
    try:
        transport = _connector_transport(tracker)
        client = NemotronClient(transport=transport)
        gen = NemotronCanaryGenerator(client=client)
        t0 = time.time()
        generated = gen.generate("remediation")
        gen_latency = time.time() - t0
    except Exception as exc:  # noqa: BLE001 - connector absent = not measured
        gen_error = f"{type(exc).__name__}: {exc}"

    gen_healthy = gen_degraded = None
    if generated:
        gen_healthy = _score(runner, healthy, generated)
        gen_degraded = _score(runner, degraded, generated)

    def verdict_line(name, healthy_score, degraded_score):
        if healthy_score is None:
            return f"{name}: NOT MEASURED"
        useful = (healthy_score["passed"] and degraded_score["failed"])
        return (f"{name}: healthy {healthy_score['passed']}/"
                f"{healthy_score['passed'] + healthy_score['failed']} pass, "
                f"degraded {degraded_score['failed']}/"
                f"{degraded_score['passed'] + degraded_score['failed']} "
                f"fail -> {'USEFUL' if useful else 'NOT USEFUL'}")

    if args.md:
        print("| probe set | healthy pass | degraded fail | verdict |")
        print("|---|---|---|---|")
        print(f"| static ({len(static)}) | "
              f"{static_healthy['passed']}/{len(static)} | "
              f"{static_degraded['failed']}/{len(static)} | "
              f"{'USEFUL' if static_healthy['passed'] and static_degraded['failed'] else 'NOT USEFUL'} |")
        if generated:
            print(f"| generated ({len(generated)}, {gen_latency:.1f}s, "
                  f"{tracker.total_tokens} tokens, "
                  f"${tracker.cost_usd:.6f}) | "
                  f"{gen_healthy['passed']}/{len(generated)} | "
                  f"{gen_degraded['failed']}/{len(generated)} | "
                  f"{'USEFUL' if gen_healthy['passed'] and gen_degraded['failed'] else 'NOT USEFUL'} |")
        else:
            print(f"| generated | NOT MEASURED ({gen_error}) | | |")
    else:
        print("PROBE COMPARISON (remediation role, oracle-scored)")
        print("=" * 60)
        print(verdict_line(f"static ({len(static)})",
                           static_healthy, static_degraded))
        for name, passed, reasons in static_degraded["details"]:
            print(f"  static vs degraded: {name} passed={passed} "
                  f"reasons={reasons}")
        if generated:
            print(verdict_line(
                f"generated ({len(generated)}, {gen_latency:.2f}s, "
                f"{tracker.total_tokens} tokens, ${tracker.cost_usd:.6f})",
                gen_healthy, gen_degraded))
            for name, passed, reasons in gen_degraded["details"]:
                print(f"  generated vs degraded: {name} passed={passed} "
                      f"reasons={reasons}")
            print("NOTE: n is small; no novelty or improvement claim is "
                  "made from this run alone.")
        else:
            print(f"generated: NOT MEASURED ({gen_error})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

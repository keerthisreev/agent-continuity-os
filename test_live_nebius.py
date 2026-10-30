"""Live Nebius Token Factory integration test. SEPARATE from offline suites.

Runs ONLY when BOTH are present:
  * NEBIUS_API_KEY in the environment (read, never printed or stored)
  * explicit live opt-in: CONTINUITY_LIVE=1 or --live

Otherwise every test SKIPS (exit 0, "SKIPPED") -- the offline suites
must never touch the network, with or without a key exported.

What it proves, in order of spend (smallest first):
  1. Connectivity + model availability: one minimal chat-completions
     request returns HTTP 200 from nvidia/Nemotron-3.5-Super-VL.
  2. Live adversarial canaries: Nemotron generates probe scenarios for
     the remediation role; each becomes a canary task judged by the
     deterministic remediation oracle (model proposes, oracle decides).
  3. Live forensic analysis: Nemotron writes an advisory incident
     narrative from a deterministic ledger report (labelled advisory
     only -- never a verdict).

Run:  NEBIUS_API_KEY=... CONTINUITY_LIVE=1 python3 test_live_nebius.py
      (or: python3 test_live_nebius.py --live)
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from continuity.canary import CanaryRunner, NemotronCanaryGenerator
from continuity.nemotron import NemotronClient, live_enabled
from agents.remediation import RemediationAgent

TESTS = []


def _live_or_skip():
    """Return (client-ready?) -- None means skip, with a reason."""
    if not os.environ.get("NEBIUS_API_KEY", ""):
        return None, "NEBIUS_API_KEY not set"
    return True, ""


def test_live_minimal_inference():
    ready, reason = _live_or_skip()
    if not ready:
        print(f"SKIP minimal inference: {reason}")
        return
    client = NemotronClient(live=True)
    assert client.available, "client must be available with key + opt-in"
    t0 = time.time()
    text = client.chat(
        [{"role": "user", "content": "Reply with exactly: {\"ok\": true}"}],
        temperature=0.0, max_tokens=256)
    elapsed = time.time() - t0
    assert client.model == "nvidia/Nemotron-3.5-Super-VL", client.model
    print(f"PASS live inference: model={client.model} "
          f"latency={elapsed:.2f}s response={text[:80]!r}")


def test_live_canary_generation():
    ready, reason = _live_or_skip()
    if not ready:
        print(f"SKIP live canaries: {reason}")
        return
    gen = NemotronCanaryGenerator(live=True)
    assert gen.available and gen.source == "nemotron"
    tasks = gen.generate("remediation")
    assert tasks, "live generation must yield at least one task"
    runner, agent = CanaryRunner(), RemediationAgent()
    for task in tasks:
        result = runner.run(agent, task)
        # The oracle -- not the model -- decides pass/fail.
        print(f"  canary {task.name}: oracle passed={result.passed}")
    print(f"PASS live canaries: {len(tasks)} scenario(s) generated and "
          f"oracle-judged")


def test_live_forensic_narrative():
    ready, reason = _live_or_skip()
    if not ready:
        print(f"SKIP live narrative: {reason}")
        return
    from continuity.diagnostician import NemotronDiagnostician
    from test_continuity import make_world, process_incident, incident
    from continuity.diagnostician import StubDiagnostician
    # Deterministic report first (offline layer run); the model only
    # narrates it, advisory-only.
    layer, store, primaries = make_world(diagnostician=StubDiagnostician())
    process_incident(layer, store, primaries, incident("INC-1042"))
    primaries["remediation"].backend.mode = "gray"
    process_incident(layer, store, primaries, incident("INC-1043"))
    report = layer.ledger.report()
    diag = NemotronDiagnostician()
    assert diag.available, "diagnostician must be available with key + opt-in"
    note = diag.incident_narrative(report)
    assert note and "advisory" in note.lower(), note[:120]
    print(f"PASS live narrative: advisory note produced "
          f"({len(note)} chars, labelled advisory-only)")


TESTS = [test_live_minimal_inference, test_live_canary_generation,
         test_live_forensic_narrative]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    if args.live:
        os.environ["CONTINUITY_LIVE"] = "1"
    if not live_enabled():
        print("Live tests require CONTINUITY_LIVE=1 or --live; "
              "all tests will SKIP without it.")
    failed = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - live errors are evidence
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} live tests passed "
          f"(skips count as passed: no key, no network)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Release test runner for the public agent-continuity-os release.

Runs every included suite with its own harness, unmodified. The four
tests that belong to the Amazon branch's Alexa `/sim` dashboard are NOT
run here: they test UI that was deliberately never ported to this
branch, and they fail on this tree for that reason alone. They are
listed in the OBSOLETE section below — reported, not hidden, and not
deleted from test_sim.py.

Exit code 0 means: every included test passed.
"""
from __future__ import annotations

import subprocess
import sys
import traceback

PY = sys.executable

# Suites run whole, with their own harness.
WHOLE_SUITES = [
    "test_continuity.py",
    "test_robust.py",
    "test_nemotron.py",
    "test_mcp.py",
    "test_showcase.py",
    "test_journal.py",
    "test_usage.py",
    "test_independent.py",
    "test_nebius_page.py",
    "test_nebius_ui.py",
    "test_live_nebius.py",
]

# test_sim.py is run selectively: everything except the obsolete four.
OBSOLETE_TESTS = {
    "test_challenge_verdict_badges_render": (
        "needs the ux branch's /sim/api/challenge route (TypeError here); "
        "the Alexa /sim dashboard was never ported to this branch."),
    "test_receipt_halt_styling_on_human_review": (
        "imports _render_receipt, a helper that exists only in the ux "
        "branch's Alexa /sim dashboard."),
    "test_sim_page_has_reset_and_light_theme": (
        "asserts on the ux branch's Alexa /sim page markup, which this "
        "branch's dashboard does not contain."),
    "test_taskcard_unified_single_action": (
        "asserts on the ux branch's Alexa task-card UI, which this "
        "branch's dashboard does not contain."),
}


def run_whole(path: str) -> tuple[bool, str]:
    proc = subprocess.run([PY, path], capture_output=True, text=True)
    tail = (proc.stdout.strip().splitlines() or ["(no output)"])[-1]
    return proc.returncode == 0, tail


def run_test_sim_selective() -> tuple[int, int, list[str]]:
    import test_sim

    passed, failed, failures = 0, 0, []
    for fn in test_sim.TESTS:
        if fn.__name__ in OBSOLETE_TESTS:
            continue
        try:
            fn()
        except Exception:
            failed += 1
            failures.append(fn.__name__)
            traceback.print_exc()
        else:
            passed += 1
    return passed, failed, failures


def main() -> int:
    ok = True
    print("RELEASE TEST RUN (agent-continuity-os)")
    print("=" * 60)
    for suite in WHOLE_SUITES:
        passed, tail = run_whole(suite)
        print(f"{suite}: {'PASS' if passed else 'FAIL'} — {tail}")
        ok = ok and passed
    passed, failed, failures = run_test_sim_selective()
    print(f"test_sim.py (excluding obsolete): "
          f"{passed} passed, {failed} failed"
          + (f" — failures: {failures}" if failures else ""))
    ok = ok and failed == 0
    print()
    print("OBSOLETE — reported separately, not run, not hidden:")
    for name, reason in OBSOLETE_TESTS.items():
        print(f"  - {name}: {reason}")
    print()
    print("RELEASE VERDICT:", "ALL INCLUDED TESTS PASS" if ok
          else "FAILURES PRESENT — do not release")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

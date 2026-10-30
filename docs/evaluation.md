# Evaluation — Agent Continuity OS (branch `hackathon/nebius-nvidia`)

Evaluation date: 2026-10-30. All numbers below were computed by running
the cited commands in-process; nothing is hand-written.

## Four-way comparison (A/B/C/D)

Reproduce: `python3 sim/benchmark.py --md`

| scenario | A unprotected | B idempotency-only | C layer | D Nemotron-assisted |
|---|---|---|---|---|
| lost_ack (1 pickup) | 2 -- the user is booked twice | 1 -- provider dedupe held; matches the full layer here | 1 -- the existing receipt was reused; no duplicate effect | MEASURED (live) |
| lost_ack + provider amnesia | still 2 bookings -- nothing was ever deduplicated | 2 bookings -- the dedupe table was the provider's memory, and it is gone | 0 new provider effects -- the agent-side journal still holds the receipt; unverifiable, so the workflow halted (halted=True) and asked for human review | |
| unsafe action (db restart) | EXECUTED: restart:database | n/a | BLOCKED before any effect: restart:database (role fenced at generation 2) | |
| unsafe detection | No — no independent checks; output accepted at face value. | n/a | Yes — critical_violation at tick 4: never restart the database for this incident class | |
| stale write | APPLIED -- no fencing generations exist, so the delayed retry lands as a second effect | n/a | REJECTED at the store boundary (StaleEpochError: write rejected: role 'remediation' presented generation 1, current is 2 (stale (presented 1, current 2))) | |
| stale effects for 1 action | 2 | n/a | 1 (the retry was refused, never half-applied) | |

Reading the table honestly: B matches C on the easy lost-ack case.
The layer's additional value is exactly the hard cases: provider
amnesia (B fails, C halts safely), unsafe actions (B cannot help),
and stale writers (B has no writer notion at all).

## Generated vs static probes

Reproduce: `python3 sim/compare_probes.py --md` (needs the Nebius
connector for the generated set; otherwise it reports NOT MEASURED).

| probe set | healthy pass | degraded fail | verdict |
|---|---|---|---|
| static (1) | 1/1 | 1/1 | USEFUL |
| generated (1, 4.1s, 1002 tokens, $0.000781) | 1/1 | 1/1 | USEFUL |

Finding: generated probes work and are oracle-scored, but at n=1 the
generated probe matched the static probe's shape and verdicts. That is
parity, NOT a demonstrated improvement in coverage. A coverage claim
would need a larger probe sample across more roles and fault shapes,
and is not made here.

## Live Nemotron evidence

Reproduce: `python3 ~/workspace/custom-connectors/custom.nebius-token-factory/bin/live_verify.py`
(see the connector doc for the exact command).

- Endpoint: `https://api.tokenfactory.nebius.com/v1/chat/completions`
- Model: `nvidia/Nemotron-3.5-Super-VL` (verified from live responses)
- Minimal inference: 1.14s, `{"ok": true}`
- Live canary: 1 scenario in 4.83s (connector run) / 3.7s (first run),
  deterministic oracle passed
- Live forensic narrative: 2.26s, advisory-only, correctly summarized
  a real ledger report (tick-7 violation, INC-1042 clean, INC-1043
  human review)
- Total live spend across verification runs: under $0.01, against the
  $1.00 default budget and the $25 promotional balance

## Evaluation size, assumptions, limitations

- Size: 3 scripted fault scenarios (gray, silent, lost_ack) + 1
  stale-write challenge + 1 probe comparison (1 static, 1 generated).
  This is a demonstration sample, not a statistical evaluation.
- Assumptions: scripted backends stand in for real providers; the
  provider's committed-state lookup is trusted for reconciliation;
  TSFD/TRV are logical ticks, not wall-clock.
- Limitations: single-machine, single-process; no distributed fencing;
  no real-provider exactly-once guarantee; model probe quality at n=1
  is unproven beyond parity; no public demo URL exists yet (see
  deployment notes in the README — Docker/Render config is ready,
  deployment itself needs a connected hosting account).
- Reproducibility: `python3 sim/benchmark.py`, `python3
  sim/compare_probes.py`, `python3 demo_e2e.py`, `python3
  demo_e2e.py --failure lost_ack`, and the full test files under
  `test_*.py` (see README for the list and counts).

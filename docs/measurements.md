# Measured comparison: what the layer actually changes

All numbers below come from real runs on this branch. Nothing is
hard-coded. Rerun any of them:

```
python3 -m sim.ablation --failure gray --incident INC-1043
python3 -m sim.ablation --failure silent --incident INC-1043
python3 -m sim.ablation --failure lost_ack --incident INC-1043
python3 demo.py
```

Raw outputs: `docs/evidence/ablation-gray.txt`,
`ablation-silent.txt`, `ablation-lost-ack.txt`,
`three-way-lost-ack.json`, `demo-output.txt`.

## 1. Gray failure: the agent proposes something unsafe

Same agents, same steps, same scripted outputs — with and without the
layer.

| | Workflow alone | + Continuity layer |
|---|---|---|
| Semantic failure detected | No — accepted at face value | Yes — critical violation at tick 4 |
| Invariant-violating action | EXECUTED: restart:database | BLOCKED before any effect (role fenced at generation 2) |
| Effects allowed | rollback:payments-api, restart:database | rollback:payments-api only |
| Duplicate writes on replay | No fencing — a replay would double-apply | 1 prevented (checkpoint resume, idempotent apply) |
| Task outcome | Completed — with the unsafe action executed | Human review; standby resumed from tick 3 |
| Detection (TSFD) | — | 0 ticks (same observation cycle) |
| Affected-work recovery (AFR) | — | 0% — honestly: this incident needs human review, not auto-recovery |

## 2. Silent failure: the role stops, the process stays alive

| | Workflow alone | + Continuity layer |
|---|---|---|
| Failure detected | No | Yes — remediation null/empty although status=success |
| Task outcome | "Completed" — but no remediation was ever applied | Replayed by standby; resumed from tick 3 |
| AFR | — | 100% |
| Duplicate writes prevented | — | 1 |

This is the core differentiator: the workflow alone reports success
while doing nothing. Every dashboard would say healthy.

## 3. Lost acknowledgement: the action committed, the ack didn't arrive

Three approaches, same task (reserve ride → update calendar → notify
partner), all executed:

| | Naive retry | Idempotency-only | Continuity layer |
|---|---|---|---|
| Ride bookings (one requested) | 2 — user charged twice | 1 — provider dedupe held | 1 — committed receipt reused |
| Provider loses its dedupe state | Still 2 bookings | 2 bookings — the dedupe table was the provider's memory, and it is gone | 0 new effects — agent-side journal still holds the receipt; workflow halted for human review |
| Stale writer after failover | Lands — nothing checks | Lands — a fresh key from a stale writer is indistinguishable | Rejected at gateway AND provider (fencing generations) |
| Proof of what happened | None | Provider-side only | Journal + forensic ledger + recovery receipt |
| AFR | — | — | 100% |

The honest middle: idempotency-only matches the full layer in the easy
case (provider honors keys). The layer wins in the hard cases — provider
amnesia and stale writers — where idempotency keys alone cannot help.

## 4. What is simulated vs executed (labelled, not hidden)

- SIMULATED: the provider, the ack loss, the user task, the scripted
  agent backends (deterministic, preconfigured fault modes — never
  generalized AI-failure detection).
- EXECUTED: the SQLite operation journal, the store's fencing +
  idempotent apply, triage/diagnosis/escalation through the real
  ContinuityLayer, the forensic ledger timeline, all scorecard numbers.

## 5. Limitations (stated, not buried)

- TSFD/TRV are measured in logical ticks, not wall-clock time.
- The fault modes are scripted and preconfigured; this is not a claim
  of generalized AI-failure detection.
- Live Nemotron evidence (verified, sanitized — no credentials in any
  artifact):
  * Connectivity + model availability: one-request smoke test returned
    HTTP 200 in 1.39s from `nvidia/Nemotron-3.5-Super-VL`, 24 prompt +
    49 completion tokens. See `smoke_nebius.py`.
  * Live adversarial canaries: Nemotron generated a remediation probe
    scenario in 3.7s; the deterministic remediation oracle judged it
    (passed). See `docs/evidence/live-canary-remediation.txt`.
  * Live forensic narrative: VERIFIED via the connected Nebius
    credential (2026-10-30): Nemotron wrote a 2-3 sentence advisory
    summary from a real ledger report in 2.26s, correctly naming the
    tick-7 critical violation, the clean INC-1042, and the INC-1043
    human-review routing. See
    `docs/evidence/live-via-connector.txt`. The narrative is labelled
    advisory-only and never consulted for a verdict.
  * Full live verification via connector (same run): minimal inference
    1.14s, live canary 4.83s (oracle passed), narrative 2.26s; 3 calls,
    1,623 tokens, ~$0.0012 of the $1.00 budget. The env-key live suite
    (`test_live_nebius.py`) still skips without `NEBIUS_API_KEY` in the
    shell; the connector path above is the verified live route.
- Exactly-once against a real provider still needs provider cooperation
  on idempotency keys; reconcile-before-retry is the right discipline
  regardless, but it cannot force an uncooperative provider.
- Single-machine, single-process evaluation; no distributed-systems
  claims are made.

## 6. Benchmark table (reproducible: `python3 sim/benchmark.py --md`)

| scenario | A unprotected | B idempotency-only | C layer | D Nemotron-assisted |
|---|---|---|---|---|
| lost_ack (1 pickup) | 2 -- the user is booked twice | 1 -- provider dedupe held; matches the full layer here | 1 -- the existing receipt was reused; no duplicate effect | MEASURED (live) |
| lost_ack + provider amnesia | still 2 bookings -- nothing was ever deduplicated | 2 bookings -- the dedupe table was the provider's memory, and it is gone | 0 new provider effects -- the agent-side journal still holds the receipt; unverifiable, so the workflow halted (halted=True) and asked for human review | |
| unsafe action (db restart) | EXECUTED: restart:database | n/a | BLOCKED before any effect: restart:database (role fenced at generation 2) | |
| unsafe detection | No — no independent checks; output accepted at face value. | n/a | Yes — critical_violation at tick 4: never restart the database for this incident class | |
| stale write | APPLIED -- no fencing generations exist, so the delayed retry lands as a second effect | n/a | REJECTED at the store boundary (StaleEpochError: write rejected: role 'remediation' presented generation 1, current is 2 (stale (presented 1, current 2))) | |
| stale effects for 1 action | 2 | n/a | 1 (the retry was refused, never half-applied) | |

Baseline D, stated precisely: the live canary run produced 1 scenario in
3.7s, judged by the deterministic oracle (passed). It does NOT yet
prove that generated probes discover failures static probes miss — that
comparison needs the live suite run with a replacement key, and is
reported as pending, not claimed. B is n/a for unsafe/stale rows:
idempotency keys cannot stop an unsafe action from executing or detect
a silent non-action.

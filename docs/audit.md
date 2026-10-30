# Repository & architecture audit — Agent Continuity OS (branch `hackathon/nebius-nvidia`)

Audit date: 2026-10-30. Scope: `continuity/` (adapter, sentinel,
canary, nemotron, fencing, checkpoint, journal, standby, ledger),
`dashboard/app.py`, `sim/` (ablation, lost_ack, journal, receipt,
scorecard), `demo.py`, and all test suites. The Amazon Alexa+
submission branch is untouched by this branch.

## Implemented and test-backed

- **Sentinel** (`continuity/sentinel.py`): four-layer health vector
  (liveness, contract, suspicion-triggered canary, business invariant)
  with hysteresis; anomaly → canary → quarantine/failover. Covered by
  test_continuity (12/12) and test_robust (9/9).
- **Fencing** (`continuity/fencing.py`): shared per-role epoch
  generations, rotated on failover and rejoin; stale writers rejected
  at the store boundary (StaleEpochError). Proven by the stale-write
  challenge and test_robust.
- **Checkpoint resume** (`continuity/checkpoint.py`, `adapter.py`):
  incident-scoped slots keyed by (agent, incident); completed-action
  merge across failover; schema-version fail-closed. Covered by
  test_continuity and test_robust.
- **Journal** (`continuity/journal.py`): SQLite operation journal with
  reconcile-before-retry; lost-ack recovery decisions recorded, never
  invented. test_journal 21/21.
- **Ledger** (`continuity/ledger.py`): forensic record (failure
  windows, affected-work map, duplicates prevented); MCP read-only
  interface (test_mcp 11/11) never writes or changes verdicts.
- **Canary oracles** (`continuity/canary.py`): behavioral invariants,
  never string matching; whoever writes a probe, the oracle judges it.
  Static probes + Nemotron path, both tested (test_nemotron 12/12).
- **Nemotron client** (`continuity/nemotron.py`): OpenAI-compatible
  POST to Token Factory, 20s timeout, 2 retries with backoff, circuit
  breaker (3 failures → 60s open), explicit live opt-in (key presence
  alone never authorizes network use). Verified live: HTTP 200, 1.39s,
  `nvidia/Nemotron-3.5-Super-VL`, 73 tokens.
- **Simulations** (`sim/`): gray/silent/lost_ack/stale_write ablations
  with baseline-vs-protected comparisons computed from the runs' own
  records. test_sim 18/22 — the 4 failures are Alexa `/sim` dashboard-UI
  tests that exist only on the Amazon branch, not regressions.

## Added during this directive (gaps found by the audit)

- **Usage guardrail** (`continuity/usage.py`): token + cost tracking
  from the API's own usage payload; $1 default budget enforced
  client-side before every real call (test_usage 8/8). Previously the
  client discarded usage entirely — spend was unmeasured.
- **Canary schema validation** (`continuity/canary.py`): model
  scenarios must carry a well-formed incident (service/symptom/
  severity strings); malformed scenarios are dropped, all-invalid
  falls back to the static probe (test_usage 8/8). Previously any
  JSON array element became a task.
- **Framework independence** (`examples/independent_booking.py`,
  `examples/wrap_independent.py`, test_independent 3/3): an untouched
  booking pipeline wrapped from outside via contract + state adapter +
  reconciling standby. Proves the layer is infrastructure, not demo
  logic.
- **Reproducible benchmark** (`sim/benchmark.py`): A/B/C table
  computed in-process via `run_challenge`, plus honest Baseline D
  status from the live evidence file.
- **Judge dashboard page** (`/nebius`): problem in 30 seconds, three
  scenario buttons that execute `run_challenge` at request time,
  Nemotron evidence, receipt, and a simulated-vs-executed disclosure
  (test_nebius_page 3/3).

## Simulated — stated, not hidden

Provider backends, ack loss, fault modes, and agent outputs are
scripted. TSFD/TRV are logical ticks, not wall-clock. Evaluation is
single-machine, single-process. The dashboard's live view replays a
scripted scenario; the `/nebius` result pages compute from those
replays, they do not observe a production system.

## Guardrail review (2026-10-30)

- **Pricing verified accurate:** $0.30/$0.90 per million input/output
  tokens, confirmed against Nebius's own blog and two independent
  catalog mirrors. Reasoning tokens arrive inside completion_tokens
  and are counted at the output rate (conservative).
- **Persistence fixed:** `UsageTracker` now loads/saves totals to an
  optional JSON file atomically (`CONTINUITY_USAGE_FILE`); previously
  a restart silently reset spend. On ephemeral hosting disks the file
  survives only within one deploy — documented, not hidden.
- **Concurrency fixed:** budget check, spend, and usage record now run
  as one serialized unit per client (`_call_lock`), and `check()`
  computes cost under the tracker's lock. Previously two threads could
  both pass the check and overshoot together.

## Architectural weaknesses (honest)

1. **No distributed fencing.** Epochs are in-process; multi-node
   deployments would need a shared fencing authority (not built).
2. **Reconciliation trusts provider-side truth.** If a provider lies
   about committed state, the layer must escalate (human review),
   never blindly retry. This is by design, and it bounds the
   exactly-once claim.
3. **Canary coverage is role-shaped.** Oracles cover the four demo
   roles; new roles need new oracles. The one observed live probe
   was generic, not sharply adversarial — model probe quality is
   unproven beyond n=1.
4. **Live forensic narrative: VERIFIED 2026-10-30 via the connected
   Nebius credential** (`docs/evidence/live-via-connector.txt`): 2.26s,
   advisory-only, correctly summarized a real ledger report. The
   env-key live suite (`test_live_nebius.py`) still skips without
   `NEBIUS_API_KEY` in the shell; the connector path is the verified
   live route.
5. **No public demo URL from this VM.** Dashboard runs locally and
   via Docker; no hosted URL exists. Judges run it from the README.

## Claims NOT supported by test evidence (do not make them)

Production impact, latency improvement percentages, generalized
AI-failure detection, multi-node behavior, real-provider exactly-once
guarantees, and any model-effectiveness claim beyond the single
observed live canary run. The benchmark table reports measured
outcomes on scripted faults only.

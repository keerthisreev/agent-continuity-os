# Prior art: Maat (Sept 2026) — comparison

Source: Uliana Elina (SynWe Group s.r.o.), "Maat: Independent Deterministic
Contract-Based Governance for Multi-Agent LLM Workflows," arXiv Sept 2026
(v2, corrected). Read in full 2026-10-19.

## What Maat is

A deterministic contract-based governance layer for multi-agent LLM workflows.
It validates agent-to-agent handoffs against a versioned contract ("anchor")
with no language model in the validation or scoring path. Seven gates: plan
validation, handoff validation, validation-depth control, role adherence and
integrity, survival/health monitoring, circuit breaker for persistently
failing agents, cross-handoff entity consistency — plus requirement primitives
(domain business rules). A CrewAI integration artifact demonstrates insertion
at framework boundaries without rewriting the workflow.

Evaluated on 6 domain workflows (6–15 agents, 522 trials) with injected
data-level defects (price drift, identity drift, payout inflation, VAT
misclassification) and a deterministic seven-check rubric.

## Honest accounting of Maat's results (v2 corrections)

- 37% of intervention halts were false alarms (35/94) — caused by validator
  defects, not model behavior. The v1 claim of gains on every workflow was
  withdrawn.
- Surviving claim, narrowed: *where halts are attributable to real defects,
  they prevent the defect from propagating.*
- Explicitly out of scope: general hallucination detection; purely semantic
  failures ("a hallucinated explanation that violates no encoded state"
  passes the layer).
- Single base model in evaluation; synthetic workflows; the conductor engine
  is proprietary/licensable (benchmarks public, internals not).
- Their own lesson, quoted: "determinism should not be confused with
  correctness — the same misconfigured rule will fail in the same way every
  time."

## Comparison

| | Maat | Agent Continuity Layer |
|---|---|---|
| Governed object | Handoff payloads between agents | Agent role health (is the role still functioning?) |
| Core failure | Corrupt data propagating downstream | Gray failure: process alive, role dead |
| Verdict substrate | Deterministic, no LLM in path | Deterministic Sentinel, LLM never load-bearing |
| On detection | Halts the chain (intervene) or warns | Fails over to hot standby; workflow continues |
| State | Stateless per-handoff checks | Continuity packages, epoch fencing, forensic ledger |
| Recovery | None — halt, optionally route to human | Verified recovery: probes must pass, rejoin at new epoch |
| False positives | 37% false-alarm halts (their own audit) | Hysteresis (one anomaly never failovers) + suspicion-triggered canaries; zero false failovers in healthy runs |
| Semantic/gray failures | Explicitly excluded | Core scope (failure mode C: plausible-but-wrong) |
| Health monitoring | G5 exists but "calibration-dependent," peripheral mechanism tests | The central mechanism: 4-layer health vector per role |
| Metrics | Rubric scores, cost delta | TSFD, TRV, AFR |
| License | Proprietary engine, public benchmarks | MIT, fully open |

## Devpost-ready paragraph

"Closest prior art is Maat (Sept 2026), which independently validates our
core architectural thesis: governance should be deterministic, with no LLM
in the verdict path. Maat guards the handoffs between agents — we guard the
agents themselves. A handoff validator cannot catch the failure where nothing
is handed off because the role silently stopped working. And where Maat halts
the workflow on a defect, the Continuity Layer fails over to a fenced standby
and keeps the workflow alive, then verifies recovery before the original
agent rejoins. Maat's own audit (37% false-alarm halts from validator
defects) is the cautionary tale our hysteresis and suspicion-triggered
canaries were designed against."

## Shared ground (cite, don't hide)

- Deterministic verifier, dumber than the verified. Their words: the design
  "separates generative reasoning from governance."
- Inserted underneath existing frameworks without rewriting agents (their
  CrewAI artifact; our adapter boundary — agents never import the layer).
- Contracts/versioned anchors must themselves be tested and versioned (their
  §4.4; our checkpoint schema versioning).

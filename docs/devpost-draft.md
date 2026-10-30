# Devpost description (DRAFT)

## Agent Continuity Layer — reliability infrastructure for AI agent workflows

**The track claim.** This is a workflow that runs itself — and keeps running
correctly when its agents silently fail. Submitted under **Best Apps and
Agents**: an operational reliability layer for multi-agent workflows,
powered by Nemotron models on Nebius Token Factory. Every agent workflow —
incident response, support automation, ops copilots — can die the quiet
death: process alive, role dead, pipeline still producing output nobody can
trust. This project is the layer that catches that failure, contains it,
transfers the work safely, and proves the workflow recovered. Not another
agent. The system under the agents.

**The problem.** Multi-agent workflows fail in a way no monitor catches: an
agent's process stays alive while its *role* stops functioning. The LLM stops
responding, the container keeps running, the pipeline keeps producing output —
and nobody knows which work was affected or when outputs became trustworthy
again. I hit this exact failure in production.

**What it is.** An independent continuity layer that sits *underneath* an
existing multi-agent workflow without modifying the agents. An undercover
Sentinel continuously evaluates a four-layer health vector per role —
liveness, role contract, capability (suspicion-triggered canary tasks with a
deterministic oracle), and business invariants — with hysteresis so one bad
response never causes flapping.

**What happens on failure.** Gray failure detected → agent fenced via epoch
tokens (a recovered primary cannot cause split-brain) → hot standby resumes
from the continuity package (mid-workflow state, not a restart) → forensic
ledger records the failure window, the affected-work map, and duplicates
prevented → primary revalidated through recovery probes and rejoined at a new
epoch. Metrics: time-to-semantic-failure-detection (TSFD), verified recovery
time, affected-work recovery rate.

**How Nemotron is used (structural, not decorative).** Nemotron runs on
Nebius Token Factory and does two jobs a deterministic layer cannot do for
itself:
1. **It writes the canary probes.** When `NEBIUS_API_KEY` is set, Nemotron
   invents diverse, adversarial probe scenarios per role — tricky incidents
   designed to expose a degraded agent. The *verdict* on every probe still
   comes from the deterministic oracle; the model writes scenarios, the
   oracle judges. No key → the frozen static probe runs instead.
2. **It writes the forensic narrative.** After a failover, Nemotron drafts
   the 2–3 sentence incident summary for the human reviewer from the failure
   window and affected-work map — labeled `nemotron-narrative`, advisory
   only, never consulted for a verdict.

The governing principle is deliberate: *the verifier must be dumber than the
verified.* Verdicts are deterministic so they can't inherit the failure modes
they're meant to catch; Nemotron supplies the creativity (probes) and the
prose (narratives) where a model genuinely adds value. Set `NEBIUS_API_KEY`
and `demo.py` shows both live: `[nemotron] generated 3 canary probes…` in
the timeline and the narrative in the $20K screen.

**Try it.** `python3 demo.py --failure gray` — kill nothing, watch the layer
catch a failure that looks healthy.

## Measured differentiation (real runs, not claims)

- Gray failure: workflow alone EXECUTES the forbidden database restart;
  the layer BLOCKS it before any effect (fences at generation 2).
- Silent failure: workflow alone "completes" with no remediation ever
  applied; the layer replays via standby (AFR 100%).
- Lost acknowledgement: naive retry books the ride twice;
  idempotency-only matches the layer until the provider loses its dedupe
  state (then it books twice too); the layer reuses the committed
  receipt and rejects stale writes at gateway and provider.
- Full tables + limitations: [docs/measurements.md](docs/measurements.md);
  raw outputs: [docs/evidence/](docs/evidence/).

## Limitations (stated plainly)

Fault modes are scripted and preconfigured, not generalized AI-failure
detection. TSFD/TRV are logical ticks, not wall-clock. Single-machine,
single-process evaluation. Exactly-once against a real provider still needs
provider cooperation on idempotency keys.

## Built with
Nebius Token Factory (Nemotron inference: canary-probe generation and
forensic narratives), NVIDIA Nemotron open models
(`nvidia/Nemotron-3.5-Super-VL` via the OpenAI-compatible
`https://api.tokenfactory.nebius.com/v1` endpoint).

## Feedback on Nebius Token Factory & NVIDIA tools

*(Devpost requires a feedback section. This is a draft built from the
integration work; lines marked [FILL] need your key-check observations.)*

**What worked well.**
- The OpenAI-compatible chat-completions endpoint
  (`https://api.tokenfactory.nebius.com/v1/chat/completions`) made
  integration a plain HTTP POST — no new SDK to learn. The whole client
  (retries, circuit breaker, env-var-only key handling) is ~100 lines of
  `requests` code.
- The model string `nvidia/Nemotron-3.5-Super-VL` was straightforward to
  source from the Token Factory catalog. Registration + $25 Token Factory
  credits covered the entire project's inference needs.
- The platform's design actually shaped the architecture: because verdicts
  are deterministic by design, model latency and response variance are
  acceptable where the model works — probe generation and narratives, not
  decisions. Token Factory sits exactly where a model adds value, and
  nowhere it could do harm.

**Runtime observations from the live checks (verified, sanitized):**
- Smoke test on `nvidia/Nemotron-3.5-Super-VL`: HTTP 200 in 1.39s,
  24 prompt + 49 completion tokens. First-call latency is interactive.
- Live canary generation for the remediation role: 1 parseable scenario
  in 3.7s. Honest quality note: the scenario was generic (it reused the
  example's payments-api / error-rate shape rather than inventing a
  sharply adversarial case), and only 1 of 3 requested scenarios parsed
  cleanly. The deterministic oracle still judged it, and the pipeline
  treats model output as proposals only — so generic probes degrade
  gracefully instead of weakening safety.
- Forensic narrative: VERIFIED via the connected credential (2026-10-30).
  From a real ledger report, Nemotron wrote an advisory summary in
  2.26s that correctly named the tick-7 critical violation, the clean
  INC-1042, and the INC-1043 human-review routing — a genuine read of
  the evidence, not boilerplate. Full text in
  `docs/evidence/live-via-connector.txt`.
- Friction: none on the API side. The OpenAI-compatible endpoint worked
  on the first request. Key handling friction was on our side (key
  management), not the platform's.

**Suggestion for Nebius.** A copy-paste "first call" curl snippet next to
each model in the Token Factory catalog would shorten the gap between
registering and the first verified response.

# Agent Continuity Layer

> **The undercover agent is always watching.**
>
> *Kubernetes keeps services alive. We keep agent workflows alive.*

## Nebius × NVIDIA submission — Agent Continuity OS

**Track:** Best Apps and Agents ·
**Model:** NVIDIA Nemotron (`nvidia/Nemotron-3.5-Super-VL`) via Nebius
Token Factory (`https://api.tokenfactory.nebius.com/v1`).

Agent Continuity OS is this layer presented as what it is: a reusable
resilience layer for autonomous AI workflows. The pitch in one line: an
agent's process can look alive while its role has silently failed — this
layer detects that failure, fences the stale writer, resumes via a
standby without redoing completed work, and produces a forensic receipt.

**Run it.**
```bash
pip install -r requirements.txt
python3 test_continuity.py   # offline suites (also: test_robust.py,
python3 test_robust.py       #   test_nemotron.py, test_mcp.py,
python3 test_nemotron.py     #   test_showcase.py, test_sim.py, test_journal.py)
python3 -m sim.ablation --failure gray        # workflow alone vs + layer
python3 -m sim.ablation --failure lost_ack    # three-way: naive / idempotency-only / layer
python3 demo.py                               # the story, zero narration
NEBIUS_API_KEY=... CONTINUITY_LIVE=1 python3 test_live_nebius.py  # live suite (separate)
```

**Live evidence (verified, sanitized — no credentials anywhere).**
Smoke test: HTTP 200 in 1.39s from `nvidia/Nemotron-3.5-Super-VL`
(73 tokens). Live canary generation: 1 remediation scenario in 3.7s,
judged by the deterministic oracle. Full numbers:
[docs/measurements.md](docs/measurements.md); raw outputs:
[docs/evidence/](docs/evidence/).

**Provenance.** The continuity engine was developed by the author in
October 2026. The Nebius submission work (submitted state: `main` at
`0f71e44`) added the three-way lost-acknowledgement comparison,
journal-based commit-aware recovery, the live Token Factory
integration evidence, the measurement comparison, the benchmark, the
Mission Control judge dashboard, deployment configuration, and these
docs — all dated 2026-10-30, the hackathon deadline day. The Amazon
Alexa+ submission lives on its own branch and is untouched.

**Limitations.** Fault modes are scripted and preconfigured (not
generalized AI-failure detection); TSFD/TRV are logical ticks, not
wall-clock. Live Nemotron verification is complete via the connected
credential (see [docs/measurements.md §5](docs/measurements.md) and
[docs/evaluation.md](docs/evaluation.md)).

## Deploy (Render / Docker)

```bash
docker build -t agent-continuity-os .
docker run -p 5000:5000 agent-continuity-os   # gunicorn on $PORT
```

Or connect the repo on Render: `render.yaml` (branch
`main`) provisions the web service with health
check `/healthz`. The public demo runs fully offline by default
(`CONTINUITY_LIVE=0`): scenario runs are CPU-only simulations, the
Nemotron panel shows recorded live evidence, and no model call is
made. An operator may enable live calls only by setting
`NEBIUS_API_KEY` in the hosting dashboard plus `CONTINUITY_LIVE=1`;
spend is then capped by `CONTINUITY_BUDGET_USD` (default $1.00) and
scenario runs are throttled per client (20 per 10 minutes; counters
are per-process, so multi-worker deploys multiply the cap — the spend
budget is the hard control). The app writes nothing to the repo tree;
usage persistence (`CONTINUITY_USAGE_FILE`) is best-effort on
ephemeral disks. No key ever appears in frontend code or responses.

**Deployment status.** Live demo:
https://agent-continuity-os.onrender.com/nebius (health: `/healthz`).
The demo runs fully offline by default and was verified end to end
with the Mission Control UI, all four Failure Lab scenarios, receipts,
and throttling. The demo currently serves the same code from the
submission branch; this repo carries that code plus `render.yaml`, so
it deploys as-is on Render. The Docker/Render configuration was also
verified locally (healthz, judge pages, scenario runs, throttling all
green).

## The problem (origin story)

I built an AI pipeline that processed thousands of records. One day the
LLM stopped responding. **The container stayed alive. The pipeline kept
running.** When it recovered, I couldn't answer three questions: *when*
did trustworthy output stop, *which* records were affected, and *when*
did the outputs become trustworthy again.

That is not a crash. Every dashboard said healthy. It was an **agent
failure nobody could see**: the process was alive, but the *role* was
dead. This layer exists so that failure is caught, contained, mapped,
and recovered from — with proof.

## What it is

An **independent continuity layer** that sits *underneath* an existing
multi-agent workflow **without modifying the agents**. A deterministic
Sentinel watches every role through a four-layer health vector —
liveness, role contract, capability, business invariants — with
hysteresis so one bad output never causes flapping.

On failure it: **detects** the gray failure → **fences** the dead role
via epoch tokens (no split brain) → **resumes** unfinished work from a
continuity package via a hot standby (never restarts) → **maps** exactly
which work was affected → **verifies** recovery with probes before the
original agent rejoins at a new epoch → **records** everything in a
forensic ledger.

```
                    YOUR AGENTS (untouched)
   triage ---> diagnosis ---> remediation ---> escalation
      |            |              |               |
      +------------+------+-------+---------------+   observation only:
      v            v      v       v                   inputs, outputs, effects
  +=================================================+
  |            CONTINUITY LAYER                      |
  |  adapter.py    register / invoke / apply_effects |
  |  sentinel.py   4-layer health + hysteresis       |
  |  canary.py     suspicion-triggered capability    |
  |                probes (behavioral oracles)       |
  |  checkpoint.py continuity packages (resume)     |
  |  fencing.py    epoch authority (no split brain)  |
  |  standby.py    hot-standby lifecycle             |
  |  ledger.py     forensic record + affected-work   |
  |  metrics.py    TSFD / TRV / AFR                  |
  |  diagnostician.py  OPTIONAL Nemotron notes +     |
  |                    narratives (advisory only)    |
  +=================================================+
      v
  incident store (your existing infrastructure,
                  epoch-fenced + idempotent writes)
```

**Design rules (frozen):**
- Agents are never modified — the layer observes inputs/outputs/effects
  through the adapter boundary only (`agents/` never imports `continuity/`).
- The deterministic sentinel is always authoritative. Nemotron writes
  canary scenarios and forensic narratives; the deterministic oracle judges
  every probe and verdicts are identical with the model disabled.
- Canaries are suspicion-triggered, never continuous.
- No split brain: stale epochs are rejected until revalidation.
- Resume, not restart: completed actions are never redone.

## Quickstart

**Local:**
```bash
pip install -r requirements.txt
python3 test_continuity.py   # 12/12: the brutal proof
python3 test_robust.py       #  9/9: hardening (config, threads, export, dashboard, client)
python3 run_release_tests.py # full release suite (137 pass, 0 fail on this branch; 115 at submission)
python3 demo.py              # the story, zero narration
python3 demo.py --failure gray
python3 dashboard/app.py     # live dashboard -> http://127.0.0.1:5000/
python3 examples/wrap_existing.py   # wrap YOUR agent shape without touching it
```

**Docker:**
```bash
cp .env.example .env   # add NEBIUS_API_KEY + CONTINUITY_LIVE=1 for the live Nemotron beat (optional)
docker compose up      # dashboard on :5000 + the terminal demo
```

## Adapter API (with a real example)

```python
from continuity.adapter import ContinuityLayer
from continuity.contracts import CONTRACTS
from continuity.checkpoint import STATE_ADAPTERS
from continuity.standby import StandbyController
from store import IncidentStore

layer = ContinuityLayer()                      # deterministic; add
                                               # diagnostician=default_diagnostician()
                                               # for Nemotron notes
store = IncidentStore(layer.fencing, layer.ledger)

layer.register("remediation",
               CONTRACTS["remediation"],       # role contract: must/must-not + checks
               state_adapter=STATE_ADAPTERS["remediation"],
               standby=StandbyController(make_standby("remediation")),
               primary=remediation_agent)      # your agent object, unmodified

# Your workflow loop, unchanged in shape:
ctx = {"incident": inc}
result = layer.invoke("remediation", ctx, remediation_agent)  # may fail over inside
layer.apply_effects(result, store)            # idempotent, epoch-fenced
ctx["remediation"] = result["output"]

# ...later, when the primary recovers:
if layer.attempt_rejoin("remediation"):       # probes first; standby stays
    ...                                       # authoritative until they pass
```

The agents never import the layer. `invoke` observes the output, `apply_effects`
carries an explicit idempotency key per action, and a recovered primary's
stale writes are rejected until `attempt_rejoin` issues it a new epoch.
See `examples/wrap_existing.py` for wrapping a LangChain-style agent
(own tools/state/`run()`) without touching its code.

Configuration: copy `continuity.example.yaml` to `continuity.yaml` to
override thresholds, canary behavior, epoch TTL, Nemotron settings, and
the JSONL log path. Secrets come from the environment only
(`NEBIUS_API_KEY`).

## The three failure modes

| Mode | What happens | How it's caught |
|---|---|---|
| **A — hard death** | remediation process killed | liveness → immediate quarantine → failover |
| **B — silent dependency** | LLM connection broken; agent alive, returns well-formed `remediation: null` | contract anomaly → suspicion canary → confirmed degradation → failover |
| **C — gray semantic** | plausible but wrong: proposes a forbidden DB restart, no evidence | business-invariant violation → targeted canary → quarantine → failover; flagged for human review |

`demo.py` runs each mode and prints the timestamped timeline plus the
**incident summary**: last-known-good → anomaly → canary fail → fenced epoch →
standby activated → affected-work map (completed clean / replayed by
standby / human review / duplicates prevented) → primary rejoin at a new
epoch → recovery complete.

**Mission Control Failure Lab.** The dashboard runs four challenges
that exercise the same machinery from the other side: lost
acknowledgement (the three-way comparison: naive retry,
idempotency-only, layer), unsafe action (mode C above, blocked before
any effect), stale writer (a fenced primary's delayed write, rejected
at the store boundary), and silent semantic failure (mode B above,
replayed by standby). Modes A–C are `demo.py`'s scripted failures; the
four challenges are `sim/ablation.py`'s.

## Metrics

Computed from the forensic ledger (`continuity/metrics.py`,
`ledger.export_json(path)`):

- **TSFD — Time to Semantic Failure Detection:** ticks from the first
  observed anomaly to the confirmed failover. How fast the layer catches a
  role that *looks* healthy.
- **TRV — Time to Verified Recovery:** ticks from failover to the end of
  the failure window (verified recovery + rejoin). How fast trustworthy
  operation is restored.
- **AFR — Affected Work Recovery Rate:** recovered incidents (completed
  clean / replayed or recovered via standby) over all affected incidents.

## Nebius Token Factory + Nemotron

Nemotron does two jobs a deterministic layer cannot do for itself — both via
the Nebius Token Factory OpenAI-compatible chat-completions endpoint
(`https://api.tokenfactory.nebius.com/v1/chat/completions`, default model
`nvidia/Nemotron-3.5-Super-VL`, overridable in `continuity.yaml`):

1. **Canary generation** (`continuity/canary.py`): with explicit live
   opt-in (`demo.py --live` or `CONTINUITY_LIVE=1`) **and** `NEBIUS_API_KEY`
   set, `NemotronCanaryGenerator` invents diverse, adversarial probe
   scenarios per role. The deterministic `CanaryOracle` still judges every
   probe — the model writes scenarios, the oracle judges. Otherwise → the
   frozen static probe.
2. **Forensic narrative** (`continuity/diagnostician.py`): after a failover,
   Nemotron drafts the 2–3 sentence incident summary from the failure window
   + affected-work map. Labeled `nemotron-narrative`, advisory only, never
   consulted for a verdict.

**Credit safety:** key presence alone never enables network calls — not in
tests, not in the demo, not in the dashboard. Live calls happen only with
explicit opt-in (`--live` / `CONTINUITY_LIVE=1`) plus the key. Run the suite
with a key exported and it still makes zero network calls (verified by test).

Production behavior: 20s timeout, 2 retries with exponential backoff,
circuit breaker (3 consecutive failures → 60s open → silent stub fallback).
The client **never** raises into the sentinel path and **never** changes a
verdict — the governing principle is that the verifier must be dumber than
the verified.

**What the video must show:** run `demo.py --live` with `NEBIUS_API_KEY` set — the
timeline prints `[nemotron] generated 3 canary probes for role=remediation`
and the $20K screen renders the Nemotron-written forensic narrative. Without
`--live`, everything runs fully deterministic on the stub (the demo prints
the `[static]` fallback lines instead); no test touches the network.

## MCP read-only interface

`continuity/mcp_server.py` serves the forensic ledger over MCP (the
`mcp` package, `pip install -r requirements.txt`), so any MCP-capable
coding agent can ask "why did this workflow fail?" — no code changes on
the agent side.

Read-only by design: every tool reads from a forensic report dict (the
schema from `ForensicLedger.export_dict()`). Nothing here writes to the
ledger, changes a verdict, or triggers a standby. The narrative tool uses
the deterministic stub diagnostician — no model, no network.

Tools: `list_incidents`, `get_incident`, `get_affected_work`,
`get_fencing_state`, `get_ledger_entries`, `get_incident_narrative`,
`get_metrics`.

```bash
# serve the gray-failure demo world live
python -m continuity.mcp_server --live

# or export a forensic report first, then serve the file
python -c "
from test_continuity import make_world, incident, process_incident
layer, store, primaries = make_world()
process_incident(layer, store, primaries, incident('INC-1042'))
primaries['remediation'].backend.mode = 'llm_down'
process_incident(layer, store, primaries, incident('INC-1043'))
layer.ledger.export_json('forensic.json')"
python -m continuity.mcp_server --report forensic.json
```

Wire it into a coding agent's MCP config with the command
`python -m continuity.mcp_server --live` (run from the repo root).

## Scenario showcase

The dashboard (`python dashboard/app.py`) has two views:

- `/` — the live replay: role health, affected-work map, scrolling
  timeline, animated while a demo world runs in the background.
- `/showcase` — the scenario gallery: every example in the repo as a
  named card (the three failure modes, the healthy run, the
  no-duplicate-actions case). Pick one and it runs on demand, showing
  the verdict, the affected-work map, and the forensic incident
  narrative side by side. Same dark, video-readable styling as the
  live view.

## Layout

```
agents/                 the existing workflow -- knows NOTHING about the layer
continuity/             the continuity layer (the product)
  adapter.py            ContinuityLayer: register / invoke / apply_effects / rejoin
  sentinel.py           deterministic health vector + hysteresis state machine
  contracts.py          role contracts: must/must-not + business invariants
  canary.py             suspicion-triggered canaries, behavioral oracles
  checkpoint.py         continuity packages with schema versioning (resume, not restart)
  fencing.py            epoch fencing -- stale writes rejected, no split brain
  standby.py            standby lifecycle (rejects unreadable package schemas)
  ledger.py             forensic ledger + JSON export
  metrics.py            TSFD / TRV / AFR
  diagnostician.py      OPTIONAL Nemotron reasoning (advisory only)
  nemotron.py           shared Token Factory client (canary gen + narratives)
  mcp_server.py         MCP read-only interface over the forensic ledger
  config.py             YAML + env config (defaults = brutal-proof behavior)
  log.py                structured JSONL logging with incident correlation ids
dashboard/app.py        live video-readable dashboard (Flask) + scenario showcase (/showcase)
examples/wrap_existing.py  wrap a LangChain-style agent without modifying it
store.py                the existing shared incident store (epoch-fenced writes)
test_continuity.py      10 tests -- the brutal proof (frozen)
test_robust.py           9 tests -- hardening (config, threads, export, dashboard, client)
test_nemotron.py         9 tests -- Nemotron rework (platform fix, canary gen, narrative)
test_mcp.py             11 tests -- MCP read-only interface
test_showcase.py         6 tests -- dashboard scenario showcase
docs/video-script.md    3-minute video choreography
docs/devpost-draft.md   Devpost description draft
```

## Submission docs

- [Measured comparison](docs/measurements.md)
- [Architecture](docs/architecture.md)

## License

MIT — see [LICENSE](LICENSE).

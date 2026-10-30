# Video storyboard — 170 seconds (under the 3-minute limit)

Record terminal output; overlay narration. No face-to-camera required.
Every shot is a real command from this branch. Total: 2:50.

## 0:00–0:15 — The failure nobody can see (origin)
**Visual:** black screen, one line of narration text.
**Narration:** "I built an AI pipeline that processed thousands of
records. One day the LLM stopped responding. The container stayed
alive. The pipeline kept running. I couldn't say when trustworthy
output stopped, or which records were affected. That is not a crash.
That is an agent failure nobody can see."

## 0:15–0:35 — The setup
**Visual:** `python3 demo.py` start; 4-agent workflow diagram
(triage → diagnosis → remediation → escalation).
**Narration:** "Four agents, one workflow. The continuity layer sits
underneath. The agents don't know it exists. Every dashboard says
healthy."

## 0:35–1:05 — Beat 1: silent failure (the core differentiator)
**Visual:** `python3 -m sim.ablation --failure silent` — split screen:
left "workflow alone", right "+ layer".
**Narration:** "The remediation agent's model dies. Its process stays
alive. Alone, the workflow reports success — and no remediation was
ever applied. With the layer, the failure is detected in the same
observation cycle, and the standby replays the work. Same agents, same
steps. Different outcome."

## 1:05–1:35 — Beat 2: gray failure (the unsafe action)
**Visual:** `python3 -m sim.ablation --failure gray` — highlight the
BLOCKED line.
**Narration:** "Now the agent goes gray: plausible, confident, and
wrong. It proposes restarting the production database. Alone, that
action executes. With the layer, the business invariant blocks it
before any effect, the role is fenced, and a human is flagged."

## 1:35–2:05 — Beat 3: lost acknowledgement (the three-way)
**Visual:** `python3 -m sim.ablation --failure lost_ack` — the
comparison table.
**Narration:** "The action commits. The acknowledgement is lost. Naive
retry books the ride twice. Idempotency keys work — until the provider
loses its memory. Then they book twice too. The layer reuses the
committed receipt, rejects the stale writer, and shows its proof."

## 2:05–2:30 — Beat 4: Nemotron, live (the model beat)
**Visual:** `NEBIUS_API_KEY=... CONTINUITY_LIVE=1 python3
test_live_nebius.py` — show the HTTP 200, the model name, the
generated canary scenario, the oracle verdict.
**Narration:** "Nemotron on Nebius Token Factory writes the adversarial
probes — but it never decides anything. Every probe is judged by a
deterministic oracle. The model proposes; the machine verifies."

## 2:30–2:50 — The receipt + close
**Visual:** `python3 demo.py` $20K screen: failure window,
affected-work map, duplicates prevented, forensic narrative.
**Narration:** "Detection, fencing, recovery, proof. Not another agent.
The system under the agents. Agent Continuity OS — a reusable
resilience layer for autonomous AI workflows."

## Production notes
- If the live beat cannot run during recording (no key on the
  recording machine), use the captured evidence files in
  `docs/evidence/` on screen instead of faking a live call. Never
  fabricate a live result.
- On-screen labels must distinguish SIMULATED (provider, ack loss,
  scripted backends) from EXECUTED (journal, fencing, ledger, oracles).
- Keep the $20K-screen framing out of the video: it is private
  decision language, not a product claim.

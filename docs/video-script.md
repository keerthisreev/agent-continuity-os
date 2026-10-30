# Demo video choreography — 3 minutes (DRAFT)

> Record `python3 demo.py` terminal output; overlay narration. Total: under 3:00.

## 0:00–0:20 — Origin story (face to camera or voiceover)

"I built an AI pipeline that processed thousands of records. One day the LLM
stopped responding. The container stayed alive. The pipeline kept running.
I didn't know which records were affected, when the model recovered, or when
the outputs became trustworthy again. That is not a crash. That's an agent
failure nobody can see."

## 0:20–0:45 — The setup

4-agent incident workflow: triage → diagnosis → remediation → escalation.
The continuity layer sits underneath — the agents don't know it exists.
"Every monitoring dashboard says healthy."

Run: `python3 demo.py --failure silent`

## 0:45–1:30 — The gray failure (the core beat)

Kill the remediation agent's LLM connection. Keep the container alive.

Show the terminal:
- process alive, heartbeat alive — but role anomaly detected
- suspicion canary FAILED
- agent fenced (epoch revoked), standby activated from last-known-good checkpoint
- workflow continues

Narration: "The container is alive. The heartbeat is alive. The role is dead.
The undercover agent caught what monitoring never could."

## 1:30–2:00 — The $20K screen

Freeze on the affected-work output:
- failure window timestamps, TSFD
- affected-work map: completed clean / replayed by standby / human review
- duplicate writes prevented

"This is what the judges should remember — not a backup agent, but the
forensics: exactly what broke, exactly what was affected, exactly what was
saved."

## 2:00–2:30 — Recovery and rejoin

Primary reconnects → transport probe → semantic recovery probe →
write authority DENIED until revalidated → rejoined at new epoch.
"Recovery complete."

## 2:30–3:00 — The layer + Nebius/NVIDIA

- Same workflow, same agents, same tools — plus the continuity layer.
- With `NEBIUS_API_KEY` set, Nemotron does two visible jobs via Nebius Token
  Factory: it *writes the canary probes* (show the `[nemotron] generated 3
  canary probes for role=remediation` timeline lines — the model invents the
  scenarios, the deterministic oracle judges them) and it *writes the
  forensic narrative* (show the FORENSIC NARRATIVE block in the $20K screen).
- Narration: "The verdicts are deterministic — the model can't inherit the
  failures it's meant to catch. But Nemotron writes the probes that hunt the
  failure, and the incident report the human reads. That's the model where it
  adds value, and nowhere it could do harm."
- Close: "Kubernetes keeps services alive. We keep agent workflows alive."

## Recording notes
- Terminal font large; scroll slow enough to read.
- The `--failure gray` mode shows the invariant-violation + human-review path.
- Keep narration under the terminal; let the timeline tell the story.

## Plan B — if the key check fails (film this instead)

If the live Nemotron call doesn't go through before filming, do NOT fake
the 2:30–3:00 beat. Film this replacement (~30s):

- Run the demo without `NEBIUS_API_KEY`. The timeline will read
  `[static] 1 deterministic canary probe for role=remediation (no key set)`
  and the narrative block will read `stub-narrative (deterministic, …)`.
- Narration: "The AI seam is a plug-in, not a dependency. The layer catches
  the failure with zero model calls — Nemotron lights up the probes and the
  forensic narrative once the key lands."
- Before submitting, soften the Devpost "Built with" claim to match what
  actually ran: keep Nebius/NVIDIA as designed integration, but remove any
  line implying a live call happened. The eligibility gate checks for real
  tool usage, not aspirational claims — an honest stub beats a failed
  eligibility check. (Filming Plan B is also the cleanest backup for the
  Sep-12-style verification the judges may run.)

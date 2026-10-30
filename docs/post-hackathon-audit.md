# Post-hackathon audit — Agent Continuity OS

**Submitted state (untouched):** `main` at
`0f71e4401ee6ec5a1f2f33296f15578e1eb45a6d` — verified unchanged at
the end of this audit (anonymous `ls-remote` agrees).
**Development branch:** `post-hackathon/v2` (all work below lives
here only).
**Scope guardrails:** no merge, no deploy, no Render change, no
change to any judging artifact on `main`. No safety logic was
changed: no oracle, sentinel, fencing, journal, or adapter verdict
rule was modified. The only production-code change is budget
accounting (`continuity/usage.py`), which records spend — it never
decides a safety verdict.

## Ranked changes (implemented on `post-hackathon/v2`)

Ranked by engineering value first; risk and evidence next. All six
are implemented and tested, not just proposed.

### 1. Budget accounting across restarts and multiple workers — HIGH value
**What:** `UsageTracker` now serializes persisted record/check
through a POSIX file lock (`<file>.lock`), reload-merges persisted
totals before mutating (element-wise max — counters only increase),
fsyncs the temp file and the directory on save, and serializes
same-process trackers sharing one file through a process-wide lock.
**Why it matters:** the submitted code lost spend when two processes
recorded concurrently (last-writer-wins on the shared file).
**Evidence:** with a start barrier forcing genuine overlap, 4
processes x 25 records on the *submitted* code end at 66 calls, not
100 — 34 records silently lost. On this branch the same test ends at
exactly 100 calls with exact token sums. Restart persistence and
cross-process `check()` visibility are also tested.
**Risk:** low-medium. The public API is unchanged and the existing
10/10 usage tests still pass, but this touches the money path, so it
deserves a read before any merge.
**Residual (not fixed):** the check-call-record window *across*
processes is still not atomic — two processes can both pass `check()`
and then both spend. Closing it needs a reservation design (reserve
estimated cost before the call, settle actual after), which is
proposed below, not implemented here.

### 2. 40-case adversarial benchmark — HIGH value
**What:** `sim/adversarial_benchmark.py` + 6 tests. Part 1: 40 oracle
cases (4 roles x 10) with verdicts derived from the oracle code
itself. Part 2: probe discrimination — context-sensitive degraded
agents (blind to one service, severity, or symptom) run against the
frozen static probe and, when live-enabled, against Nemotron probes.
**Evidence:** 40/40 cases match expected verdicts; the static probe
catches 3/4 blind agents per role (the symptom-latency blind spot
escapes it — the fixed CANARY-1 context has an error-rate symptom,
not latency). The Nemotron arm reports "skipped" offline; it never
fabricates numbers. Two runs are byte-identical.
**Risk:** low. New code and tests only; no oracle or verdict logic
touched. The benchmark *reports* oracle gaps (see §Findings) instead
of changing verdicts to look better.

### 3. Resilience regression tests — MEDIUM-HIGH value
**What:** `test_resilience_v2.py` (5 tests). Fencing unit test (stale
generation rejected, current accepted); journal crash test (a child
process records intent + commit, then `os._exit(0)` without close —
the reopened journal still holds the committed receipt and the
pending intent); 8-thread concurrent journal writes (80/80 intents,
none lost); 4 parallel lost-ack challenges (distinct incidents, all
completed clean, no cross-talk); 4 concurrent invokes on one shared
layer (all outputs correct).
**Risk:** low. Tests only, locking in behavior the code already had.

### 4. Determinism locks — MEDIUM-HIGH value
**What:** `test_determinism.py` (2 tests). Two diagnosticians
returning different prose produce identical failover/rejoin verdicts
and identical ledger timelines except for the narrative entries; and
model-written canary probes, judged by the deterministic oracle, give
the same verdicts as the frozen static probe for the same agent
behavior.
**Risk:** low. Tests only. This is the executable form of the
project's core claim (model proposes, oracle judges, layer decides).

### 5. Second independent workflow — MEDIUM value
**What:** `examples/independent_support_pipeline.py` (three
`run(ticket)` agents, zero continuity imports) +
`examples/independent_support.py` (wrap code) + 5 tests. A
triage -> diagnosis -> escalation pipeline with no external effects,
a different agent shape from the booking provider, chained
layer-side (the escalation summary is enriched with the diagnosis
root cause in the shim, not the agent). Degraded diagnosis fails
over to a healthy standby; the final diagnosis is provably the
standby's output.
**Risk:** low. New example + tests only.

### 6. README + evaluation doc corrections — MEDIUM value
**What:** Quickstart `test_continuity` 10/10 -> 12/12 + release-runner
line (137 pass on this branch, 115 at submission); the two duplicate
Provenance paragraphs merged into one (submitted SHA recorded);
a Failure Lab mapping paragraph resolving the "three failure modes"
(`demo.py`) vs "four challenges" (`sim/ablation.py`) confusion;
`docs/evaluation.md` size line updated to 4 challenges + benchmark +
probe comparison, the stale "no public demo URL exists yet" replaced
with the live demo URL, and the reproduction path fixed from an
author-local connector path (which does not exist in this repo) to
the recorded evidence files + `test_live_nebius.py`.
**Risk:** low. Docs only, every number re-verified. The
`devpost-draft` and video docs were deliberately left untouched
(they are judging narrative, not living docs).

## Findings reported, NOT fixed (need explicit approval)

### F1. Oracle leniency gaps (5 benchmark cases)
- Whitespace-only values pass the key oracles (`severity: " "`,
  `root_cause: " "`, `summary: " "`, both escalation keys) — the
  oracles check truthiness, not stripped content.
- A restart aimed at target `"db"` evades the forbidden-action check,
  which matches `target == "database"` exactly.
- Proposed fix (not implemented): strip string values in the key
  oracle; normalize (strip/lower) the restart target before the
  exact-match check. Do NOT expand to alias matching ("db" ~>
  "database") without review — that changes the verdict semantic.
- Value: medium. Risk: medium (changes verdicts — needs deliberate
  review, which is why the benchmark pins the current behavior with
  an exact gap list that fails loudly if verdicts change).
- Evidence: the 5 gap cases in the benchmark, asserted exact.

### F2. Cross-process check-call-record window
- As §1 notes: two processes can both pass `check()` and both spend.
  The per-process budget and the provider balance remain the
  backstops. A reservation design (reserve-then-settle) would close
  it.
- Value: medium. Risk: medium-high (touches the live spend path).
  No test demonstrates an actual overshoot — the window is real but
  narrow, and it is documented, not hidden.

### F3. Ephemeral-disk usage persistence (unchanged, documented)
- On Render free instances the usage file survives only within one
  deploy; restarts/redeploys reset the local count. The provider
  balance remains the backstop. This is a hosting property, not a
  code defect, and is already documented in the README and
  `usage.py`.

## Test results (exact)

`python3 run_release_tests.py` on `post-hackathon/v2`:
12/12 continuity, 9/9 robust, 12/12 nemotron, 11/11 mcp, 6/6
showcase, 21/21 journal, 10/10 usage, 4/4 usage v2, 3/3 independent,
5/5 independent v2, 5/5 resilience v2, 6/6 adversarial benchmark,
2/2 determinism, 5/5 nebius page, 5/5 nebius ui, 3/3 live (skips:
no key, no network), 18/18 test_sim (excluding obsolete).
**Total: 137 pass, 0 fail.** The 4 obsolete `test_sim` tests are
still excluded and reported separately, exactly as at submission
(they test the Amazon branch's Alexa `/sim` UI, never ported here).
Submission total was 115; the +22 are the new suites above.

## Not done (awaiting explicit authorization)
- No merge of `post-hackathon/v2` into `main`.
- No deploy, no Render change, no judging-artifact change.
- No oracle change (F1), no reservation design (F2).
- The Nemotron arm of the benchmark has not been run live (needs a
  key + explicit opt-in; offline it honestly reports "skipped").

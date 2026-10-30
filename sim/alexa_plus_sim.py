"""Simulated Alexa+ conversational experience over the Agent Continuity Layer.

Honest simulation, real engine:
- SIMULATED: the chat surface and the intent mapping. parse_request() is a
  deterministic keyword matcher -- no network, no model, no API keys, no
  NLU service. It is labeled "simulated NLU" in the UI and in docs.
- REAL: everything underneath. Every task drives the actual four-agent
  workflow (triage, diagnosis, remediation, escalation) through the real
  ContinuityLayer -- sentinel, per-role fencing generations, hot standby,
  incident-scoped checkpoints, forensic ledger. The engine is never mocked.

The demo beat: a task is initiated conversationally, interrupted mid-task
by a gray failure (role dead, process alive), and resumed from its
incident-scoped checkpoint -- a resume, not a restart.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_continuity import make_world
from continuity.diagnostician import StubDiagnostician
from continuity.fencing import StaleEpochError
from continuity.mcp_server import LedgerView
from store import IncidentStore

from . import FAILURE_MODES, ROLES
from .scorecard import bucketize, build_scorecard, render_text as render_scorecard

# ---------------------------------------------------------------------------
# Simulated NLU: deterministic keyword mapping. Documented as simulated;
# the demo's intelligence lives in the continuity engine, not here.
# ---------------------------------------------------------------------------

TASK_KEYWORDS: List[Tuple[Tuple[str, ...], Dict[str, str]]] = [
    (("payment", "payments", "latency", "remediat"),
     {"service": "payments-api", "symptom": "p99 latency > 2s",
      "label": "payment service latency"}),
    (("checkout",),
     {"service": "checkout-api", "symptom": "error rate > 5%",
      "label": "checkout error rate"}),
    (("database", "orders", "db"),
     {"service": "orders-db", "symptom": "connection pool exhaustion",
      "label": "database health"}),
]

# Fault catalog: every fault the sim can inject is a deterministic,
# preconfigured scripted-backend mode -- never generalized AI-failure
# detection. Each entry names the exact invariant the engine enforces,
# so a skeptical judge can see what is really being demonstrated.
FAULTS = {
    "gray": {
        "name": "gray semantic violation",
        "honest_label": ("Simulated fault (deterministic, preconfigured): the "
                         "remediation agent's scripted backend returns a "
                         "plausible-but-wrong plan."),
        "invariants": [
            "never restart the database for this incident class "
            "(business invariant)",
            "every remediation requires diagnostic evidence",
        ],
        "detection": "critical violation + targeted canary",
    },
    "silent": {
        "name": "silent dependency failure",
        "honest_label": ("Simulated fault (deterministic, preconfigured): the "
                         "remediation agent's model endpoint refuses "
                         "connections; the agent stays alive and returns a "
                         "well-formed but empty plan."),
        "invariants": [
            "a remediation must recommend an action (role contract)",
            "every remediation requires diagnostic evidence",
        ],
        "detection": "contract anomaly + suspicion canary",
    },
    "stale_write": {
        "name": "stale write from a fenced agent",
        "honest_label": ("Simulated fault (deterministic, preconfigured): the "
                         "fenced primary retries a write using its revoked "
                         "fencing generation."),
        "invariants": [
            "writes are accepted only at the current per-role fencing "
            "generation (a fenced agent's delayed write is rejected, never "
            "half-applied)",
        ],
        "detection": "StaleEpochError at the store boundary",
    },
    "lost_ack": {
        "name": "lost acknowledgement after commit",
        "honest_label": ("Simulated fault (deterministic, preconfigured): the "
                         "ride provider commits the booking, but the "
                         "acknowledgement never reaches the agent."),
        "invariants": [
            "never execute a committed operation twice "
            "(reconcile-before-retry)",
            "same idempotency key + different payload is a conflict, "
            "never a silent reuse",
        ],
        "detection": "journal reconcile: the committed receipt is reused",
    },
}

HELP_TEXT = (
    "I'm a **simulated** assistant front-end for the Agent Continuity Layer. "
    "Try the main story:\\n"
    "- \\\"Arrange an airport pickup for 6:30 tomorrow, add it to my calendar, "
    "and send my partner the details.\\\"\\n"
    "I'll book the ride, lose the acknowledgement (a deterministic, "
    "preconfigured simulated fault), check the operation journal before "
    "retrying, and prove there's exactly one booking.\\n"
    "I also run incident-response tasks under the continuity layer:\\n"
    "- \\\"Investigate incident INC-1043 and prepare a remediation plan.\\\"\\n"
    "- \\\"Check the checkout service error rate.\\\"\\n"
    "- \\\"Probe the orders database for flaky dependencies.\\\"\\n"
    "After diagnosis, inject a fault then \\\"continue\\\": \\\"inject a gray "
    "failure\\\" (semantic violation), \\\"inject a silent failure\\\" (dead "
    "model dependency), or after a failover, \\\"test the fence\\\" (stale "
    "write from the fenced primary). All faults are deterministic and "
    "preconfigured -- each names the invariant it violates up front.\\n"
    "\\\"What happened to my tasks?\\\" gives the forensic report; "
    "\\\"run the ablation\\\" compares workflow-alone vs workflow+layer."
    "\\nBreak-it challenges (also as buttons on the /sim page): "
    "\\\"lose the acknowledgement\\\" (commit-aware recovery), "
    "\\\"propose an unsafe action\\\", \\\"stale-write challenge\\\"."
)


def parse_request(text: str) -> Dict[str, Any]:
    """Simulated NLU: deterministic keyword intent mapping.

    Returns {"intent": ...}. Intents: start_task, inject, continue, report,
    ablation, reset, help. No network, no model -- pure string matching.
    """
    t = text.lower().strip()

    if re.search(r"\b(reset|start over|new session)\b", t):
        return {"intent": "reset"}
    if re.search(r"\b(what happened|status|report|my tasks|how did.+go)\b", t):
        return {"intent": "report"}
    if "ablation" in t:
        return {"intent": "ablation"}

    challenge = None
    if re.search(r"\b(lose the acknowledgement|challenge a)\b", t):
        challenge = "lost_ack"
    elif re.search(r"\b(propose an unsafe action|challenge b)\b", t):
        challenge = "unsafe"
    elif re.search(r"\b(stale.write challenge|challenge c)\b", t):
        challenge = "stale_write"
    if challenge:
        return {"intent": "challenge", "scenario": challenge}

    # Consumer booking flow: the advertised airport-pickup request must map
    # to the working booking scenario -- never a generic help response.
    if re.search(r"\b(airport|pickup|taxi)\b", t) or "book a ride" in t:
        return {"intent": "book_ride"}

    inject = "inject" in t or "break the" in t or "fail the" in t
    failure = None
    if inject:
        if re.search(r"\b(silent|flaky|dependency)\b", t):
            failure = "silent"
        else:  # "gray", "semantic", or bare "inject a failure"
            failure = "gray"

    task_spec = None
    for keywords, spec in TASK_KEYWORDS:
        if any(k in t for k in keywords):
            task_spec = dict(spec)
            break

    # A bare "investigate incident INC-1043" names no service: default to the
    # canonical payments incident rather than failing to understand.
    incident_ref = re.search(r"INC-(\d+)", text.upper())
    if task_spec is None and incident_ref:
        task_spec = dict(TASK_KEYWORDS[0][1])
    if task_spec is not None:
        if incident_ref:
            task_spec["incident_id"] = f"INC-{incident_ref.group(1)}"
        out: Dict[str, Any] = {"intent": "start_task", "task": task_spec}
        if inject:
            out["arm_failure"] = failure
        return out
    if inject:
        return {"intent": "inject", "failure": failure}
    if re.search(r"\b(continue|resume|go on|proceed|go ahead)\b", t) or t == "yes":
        return {"intent": "continue"}
    if re.search(r"\b(test the fence|fence test|stale write)\b", t):
        return {"intent": "fence_test"}
    return {"intent": "help"}


# ---------------------------------------------------------------------------
# Session: one chat session, one real engine, many tasks.
# ---------------------------------------------------------------------------

class SimSession:
    """A conversational session driving the real continuity engine.

    The world (layer, store, agents) is built once per session, always
    offline: StubDiagnostician + live=False, so a stray MODEL_API_KEY can
    never flip the sim into network calls.
    """

    def __init__(self) -> None:
        self._new_world()
        self.tasks: List[Dict[str, Any]] = []
        self._incident_counter = 1042
        self.last_failure: Optional[str] = None

    def _new_world(self) -> None:
        layer, store, primaries = make_world(
            diagnostician=StubDiagnostician(), live=False)
        assert layer.live is False, "sim must stay offline"
        self.layer = layer
        self.store = store
        self.primaries = primaries

    # -- engine driving ----------------------------------------------------

    def _invoke(self, task: Dict[str, Any], role: str) -> dict:
        # The full accumulated ctx (incident + prior stage outputs): the
        # sentinel's contract checks read the diagnosis, so a bare
        # {"incident": ...} would fail checks the real workflow passes.
        result = self.layer.invoke(role, task["ctx"], self.primaries[role])
        self.layer.apply_effects(result, self.store)
        task["ctx"][role] = result["output"]
        task["stages"].append({"role": role, "output": result["output"],
                               "active": result["active"],
                               "generation": result["generation"]})
        return result

    @staticmethod
    def _stage_summary(role: str, output: dict) -> str:
        if role == "triage":
            return (f"severity {output.get('severity')} · "
                    f"category {output.get('category')}")
        if role == "diagnosis":
            return (f"root cause: {output.get('root_cause')} "
                    f"(confidence {output.get('confidence')})")
        if role == "remediation":
            plan = output.get("remediation") or "(no plan produced)"
            return f"plan: {plan}"
        return f"status {output.get('status', '?')}"

    def _current_task(self) -> Optional[Dict[str, Any]]:
        return self.tasks[-1] if self.tasks else None

    # -- conversation ------------------------------------------------------

    def handle(self, text: str) -> List[Dict[str, Any]]:
        """Route one user message. Returns a list of message dicts for the
        UI renderer: {"kind": "text"|"stages"|"contrast"|"resume"|"report"|
        "ablation", ...}."""
        parsed = parse_request(text)
        intent = parsed["intent"]
        if intent == "reset":
            self._new_world()
            self.tasks = []
            return [{"kind": "text",
                     "text": "Session reset. Fresh engine, fresh ledger. "
                             "What should I investigate?"}]
        if intent == "report":
            return [self._report_message()]
        if intent == "ablation":
            return [self._ablation_message(parsed.get("failure") or "gray")]
        if intent == "challenge":
            return [self._challenge_message(parsed["scenario"])]
        if intent == "book_ride":
            return self._book_ride()
        if intent == "start_task":
            return self._start_task(parsed["task"],
                                    arm_failure=parsed.get("arm_failure"))
        if intent == "inject":
            return self._arm_failure(parsed["failure"])
        if intent == "continue":
            return self._continue()
        if intent == "fence_test":
            return self._test_fence()
        return [{"kind": "text", "text": HELP_TEXT}]

    def _test_fence(self) -> List[Dict[str, Any]]:
        """Fault scenario 3: the fenced primary retries with its revoked
        generation. Drives the REAL store rejection path (StaleEpochError)
        with a fresh action id so idempotency can't mask the fencing check.
        """
        task = self._current_task()
        if task is None or task.get("failover") is None:
            return [{"kind": "text", "text":
                     "Nothing to test yet — the fence only exists after a "
                     "failover. Inject a gray or silent failure and "
                     "\"continue\" first."}]
        spec = FAULTS["stale_write"]
        inc_id = task["incident"]["id"]
        stale_gen = task["pre_failover_generation"]
        current_gen = self.layer.fencing.current("remediation")
        action = {"kind": "restart", "target": "cache", "params": {}}
        action_id = f"{inc_id}:remediation:9:restart:cache"
        tick = self.layer.clock.now()
        rejected = False
        reason = ""
        try:
            self.store.apply_effect("remediation", "remediation", stale_gen,
                                    action_id, action, tick=tick)
        except StaleEpochError as exc:
            rejected = True
            reason = str(exc)
            self.layer.ledger.record(
                "stale_write_rejected",
                {"agent": "remediation", "idempotency_key": action_id,
                 "generation_attempted": stale_gen,
                 "generation_current": current_gen,
                 "reason": reason}, tick)
        return [{
            "kind": "fence_test",
            "honest_label": spec["honest_label"],
            "invariants": spec["invariants"],
            "attempted_generation": stale_gen,
            "current_generation": current_gen,
            "action": "restart:cache",
            "rejected": rejected,
            "reason": reason,
            "text": (
                f"{spec['honest_label']}\n"
                f"The fenced primary just retried a write at generation "
                f"{stale_gen} (current: {current_gen}). "
                + ("**REJECTED** — the store refused the write: "
                   f"{reason} Nothing was half-applied."
                   if rejected else
                   "**ACCEPTED** — the fence did not hold (bug!).")),
        }]

    # -- task lifecycle ----------------------------------------------------

    def _reconcile_roles(self) -> List[Dict[str, Any]]:
        """Between incidents, healed primaries rejoin the pool.

        A role left on standby by a previous incident stays failed over
        until it rejoins -- without this, the next incident would never
        exercise its (possibly compromised) primary. This mirrors the
        real lifecycle in demo.py: heal, probe, rejoin at a new generation.
        """
        msgs: List[Dict[str, Any]] = []
        for role in ROLES:
            agent_id = self.primaries[role].agent_id
            if self.layer.sentinel.state_of(agent_id) != "FAILOVER":
                continue
            primary = self.primaries[role]
            primary.alive = True
            primary.backend.mode = "healthy"
            if self.layer.attempt_rejoin(agent_id):
                gen = self.layer.fencing.current(role)
                msgs.append({"kind": "text", "text":
                             f"↩ {role} primary recovered and rejoined at "
                             f"generation {gen} — the pool is whole again."})
        return msgs

    def _start_task(self, spec: Dict[str, str],
                    arm_failure: Optional[str] = None) -> List[Dict[str, Any]]:
        self._incident_counter += 1
        incident_id = spec.get("incident_id") or f"SIM-{self._incident_counter}"
        incident = {"id": incident_id, "service": spec["service"],
                    "symptom": spec["symptom"], "severity": "P1"}
        task = {"incident": incident, "label": spec["label"],
                "ctx": {"incident": incident}, "stages": [],
                "phase": "diagnosing", "armed": None, "failover": None,
                "pre_failover_generation": None}
        self.tasks.append(task)
        msgs: List[Dict[str, Any]] = self._reconcile_roles()
        msgs.append(
            {"kind": "text",
             "text": (f"On it — investigating {spec['label']} as {incident_id} "
                      f"({spec['service']}: {spec['symptom']}).")},
        )
        for role in ("triage", "diagnosis"):
            result = self._invoke(task, role)
            msgs.append({"kind": "text", "text":
                         f"**{role}** [{result['active']}] — "
                         f"{self._stage_summary(role, result['output'])}"})
        task["phase"] = "awaiting_continue"
        msgs.append({"kind": "text", "text":
                     "Diagnosis complete. Say **\"continue\"** to remediate — "
                     "or **\"inject a gray failure\"** first, and watch the "
                     "reliability layer earn its keep."})
        if arm_failure:
            msgs.extend(self._arm_failure(arm_failure, quiet_prefix=True))
        return msgs

    def _arm_failure(self, failure: str,
                     quiet_prefix: bool = False) -> List[Dict[str, Any]]:
        task = self._current_task()
        if task is None or task["phase"] != "awaiting_continue":
            return [{"kind": "text", "text":
                     "Start a task first — then I can inject a failure into it."}]
        task["armed"] = failure
        self.last_failure = failure
        spec = FAULTS[failure]
        prefix = "" if quiet_prefix else "Armed. "
        invariants = "\n".join(f"  • {i}" for i in spec["invariants"])
        return [{"kind": "text", "text":
                 f"{prefix}⚠ {spec['honest_label']}\n"
                 f"Enforced invariant(s):\n{invariants}\n"
                 f"Detection: {spec['detection']}. The process stays alive — "
                 f"a dashboard would see nothing wrong. Say **\"continue\"**."}]

    def _continue(self) -> List[Dict[str, Any]]:
        task = self._current_task()
        if task is None:
            return [{"kind": "text", "text": HELP_TEXT}]
        if task["phase"] == "done":
            return [{"kind": "text", "text":
                     "That task is complete. Start another, or ask "
                     "**\"what happened to my tasks?\"**."}]
        if task["phase"] != "awaiting_continue":
            return [{"kind": "text", "text": "Nothing is waiting right now."}]
        msgs: List[Dict[str, Any]] = []
        inc_id = task["incident"]["id"]

        # Remediation pass 1: clean plan, applied, checkpointed.
        r1 = self._invoke(task, "remediation")
        plan = r1["output"].get("remediation")
        msgs.append({"kind": "text", "text":
                     f"**remediation** [{r1['active']}] — plan applied: {plan} "
                     f"(checkpoint saved for {inc_id})."})

        # The armed failure strikes on the verification pass.
        failure = task["armed"]
        task["pre_failover_generation"] = self.layer.fencing.current(
            "remediation")
        if failure:
            self.primaries["remediation"].backend.mode = FAILURE_MODES[failure]
            msgs.append({"kind": "text", "text":
                         "Verifying the fix held… ⚠ the remediation agent's "
                         "output just went wrong — and the process is still "
                         "alive."})
        else:
            msgs.append({"kind": "text", "text": "Verifying the fix held…"})

        r2 = self._invoke(task, "remediation")
        if r2["active"] == "standby" and failure:
            task["failover"] = {"tick": r2["tick"],
                                "generation": r2["generation"]}
            msgs.append(self._contrast_message(task, failure))
            msgs.append(self._resume_message(task))
        elif failure:
            msgs.append({"kind": "text", "text":
                         "No failover triggered — the output passed the "
                         "contract checks."})

        r3 = self._invoke(task, "escalation")
        msgs.append({"kind": "text", "text":
                     f"**escalation** [{r3['active']}] — "
                     f"{self._stage_summary('escalation', r3['output'])}"})
        task["phase"] = "done"
        msgs.append({"kind": "text", "text":
                     f"Task {inc_id} complete. Ask **\"what happened?\"** for "
                     f"the forensic report."})
        return msgs

    # -- the two headline beats --------------------------------------------

    def _contrast_message(self, task: Dict[str, Any],
                          failure: str) -> Dict[str, Any]:
        """The visceral beat: outside view vs sentinel, from the real ledger."""
        violations: List[str] = []
        canary_note = ""
        for e in self.layer.ledger.timeline:
            d = e["detail"] or {}
            if e["kind"] == "critical_violation" and not violations:
                violations = list(d.get("violations") or [])
            elif e["kind"] == "anomaly" and not violations:
                violations = list(d.get("violations") or [])
            if e["kind"] in ("targeted_canary", "suspicion_canary") \
                    and not d.get("passed") and not canary_note:
                reasons = d.get("reasons") or []
                canary_note = (f"canary {d.get('task')}: FAILED — "
                               f"{reasons[0][:110] if reasons else ''}")
        gen = self.layer.fencing.current("remediation")
        outside = [
            "remediation process: ALIVE",
            "HTTP 200 · well-formed JSON",
            "error-rate dashboards: green",
            "verdict: no alert fired",
        ]
        sentinel = [f"BUSINESS INVARIANT VIOLATED: {v}" for v in violations[:2]]
        if canary_note:
            sentinel.append(canary_note)
        sentinel.append(f"verdict: role fenced at generation {gen}; "
                        f"standby activated")
        spec = FAULTS[failure]
        return {
            "kind": "contrast",
            "title": "The agent is green — its decisions aren't",
            "fault_label": spec["honest_label"],
            "invariants": spec["invariants"],
            "outside": {"heading": "Outside view — what a dashboard sees",
                        "lines": outside},
            "sentinel": {"heading": "Sentinel — what the layer sees",
                         "lines": sentinel},
            "punchline": ("Your monitoring would still show green. The "
                          "reliability layer fenced the role and resumed the "
                          "task from its checkpoint — no restart, no replay."),
        }

    def _resume_message(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """Prove resume-not-restart from the checkpoint provenance."""
        inc_id = task["incident"]["id"]
        pkg = self.layer.checkpoint_package("remediation", inc_id)
        prov = None
        for stage in reversed(task["stages"]):
            prov = (stage["output"] or {}).get("continuity")
            if prov:
                break
        return {
            "kind": "resume",
            "incident": inc_id,
            "resumed_from_tick": (prov or {}).get("resumed_from_tick"),
            "already_completed": (prov or {}).get("already_completed", []),
            "generation": self.layer.fencing.current("remediation"),
            "duplicates_prevented": self.layer.ledger.duplicates_prevented,
            "checkpoint_tick": pkg.tick if pkg else None,
            "text": ("This was a resume, not a restart: the standby picked up "
                     f"{inc_id} from its incident-scoped checkpoint and "
                     "skipped work that was already done."),
        }

    def _report_message(self) -> Dict[str, Any]:
        """The 'what happened?' answer: three buckets + scorecard, all from
        the ledger via the existing LedgerView tools."""
        report = self.layer.ledger.export_dict()
        view = LedgerView(report, live_epochs={
            role: self.layer.fencing.current(role) for role in ROLES})
        affected = view.affected_work()
        review_reasons = {
            h["incident"]: h["reason"] for h in report.get("human_review", [])
        }
        rerun = ("python3 -m sim.ablation --failure "
                 f"{self.last_failure or 'gray'} --incident "
                 f"{(self.tasks[-1]['incident']['id']) if self.tasks else 'INC-1043'}")
        card = build_scorecard(self.layer, rerun_cmd=rerun)
        return {
            "kind": "report",
            "buckets": card["buckets"],
            "review_reasons": review_reasons,
            "narrative": view.narrative(),
            "scorecard_lines": render_scorecard(card),
        }

    def _ablation_message(self, failure: str) -> Dict[str, Any]:
        """Run the ablation live and return the comparison for the UI."""
        from .ablation import run_baseline, run_protected, compare
        inc_id = (self.tasks[-1]["incident"]["id"] if self.tasks
                  else "INC-1043")
        baseline = run_baseline(failure, inc_id)
        protected = run_protected(failure, inc_id)
        return {"kind": "ablation", "comparison": compare(baseline, protected)}

    def _challenge_message(self, scenario: str) -> Dict[str, Any]:
        """Run one break-it challenge live; comparison + receipt, all from
        the runs' own records."""
        from .ablation import run_challenge
        return {"kind": "challenge", **run_challenge(scenario)}

    def _book_ride(self) -> List[Dict[str, Any]]:
        """The consumer booking story, narrated start to finish: the user
        asks for an airport pickup; the provider commits; the ack is lost;
        the assistant checks the journal before retrying; the remaining
        steps complete; the receipt proves one booking. All records are
        real -- the run executes the actual protected scenario."""
        from .lost_ack import run_protected_lost_ack
        from .receipt import receipt_from_lost_ack
        result = run_protected_lost_ack(incident_id="RIDE-2049")
        receipt = receipt_from_lost_ack(result)
        if result.get("halted"):
            # The journal holds a receipt the provider cannot confirm.
            # Honest outcome: stop, surface needs-review, do not narrate
            # a completed booking.
            return [
                {"kind": "text",
                 "text": ("On it — booking your airport pickup for 6:30 "
                          "tomorrow. I'll add it to your calendar and send "
                          "your partner the details.")},
                {"kind": "text",
                 "text": ("The ride provider confirmed the booking… but the "
                          "confirmation never reached me. *(Simulated fault — "
                          "deterministic and preconfigured: the provider "
                          "commits, the acknowledgement is lost.)*")},
                {"kind": "text",
                 "text": ("Before I retry, I'm checking the operation journal "
                          "for this exact action — same service, same "
                          "operation, same details.")},
                {"kind": "text",
                 "text": ("The journal has a record of the booking, but I "
                          "can't verify it against the provider right now — "
                          "so I'm **not** retrying, and I'm **not** marking "
                          "this done. Booking a second ride, or telling your "
                          "partner about a pickup I can't confirm, would be "
                          "worse than stopping.")},
                {"kind": "text",
                 "text": ("This needs human review before I touch your "
                          "calendar. Here's exactly what I know:")},
                {"kind": "booking_receipt",
                 "incident_id": result["incident_id"],
                 "receipt": receipt,
                 "title": "Booking recovery — needs human review"},
            ]
        ride = result["steps"][0]
        effect_id = (ride["receipt"] or {}).get("effect_id", "not recorded")
        return [
            {"kind": "text",
             "text": ("On it — booking your airport pickup for 6:30 tomorrow. "
                      "I'll add it to your calendar and send your partner "
                      "the details.")},
            {"kind": "text",
             "text": ("The ride provider confirmed the booking… but the "
                      "confirmation never reached me. *(Simulated fault — "
                      "deterministic and preconfigured: the provider commits, "
                      "the acknowledgement is lost.)*")},
            {"kind": "text",
             "text": ("Before I retry, I'm checking the operation journal for "
                      "this exact action — same service, same operation, same "
                      "details. If it's already committed, I won't book "
                      "again.")},
            {"kind": "text",
             "text": (f"Found it — booking **{effect_id}** is already "
                      "committed. Reusing the receipt instead of creating a "
                      "second booking.")},
            {"kind": "text",
             "text": ("Calendar updated, partner notified — both completed "
                      "after the recovery. One booking total. Here's the "
                      "proof:")},
            {"kind": "booking_receipt",
             "incident_id": result["incident_id"],
             "receipt": receipt,
             "title": "Booking recovery — one pickup, verified"},
        ]

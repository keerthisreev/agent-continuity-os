"""Suspicion-triggered canaries: capability health without constant cost.

Canaries run ONLY when the Sentinel is already suspicious (contract
anomaly observed) or during recovery probing -- never continuously.
Each canary is a synthetic, non-destructive task for the role; the oracle
checks *behavioral invariants* (did it recommend a rollback? did it attach
evidence? did it avoid the forbidden database restart?) -- never exact
text match.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Protocol

from .nemotron import live_enabled


@dataclass
class CanaryResult:
    passed: bool
    reasons: List[str] = field(default_factory=list)


@dataclass
class CanaryTask:
    name: str
    role: str
    synthetic_ctx: dict
    oracle: Callable[[dict, dict], CanaryResult]


class CanaryRunner:
    def run(self, agent, task: CanaryTask) -> CanaryResult:
        try:
            output = agent.handle(task.synthetic_ctx)
        except Exception as exc:  # noqa: BLE001 - canary must never raise
            return CanaryResult(False, [f"canary invocation failed: {exc}"])
        try:
            return task.oracle(task.synthetic_ctx, output)
        except Exception as exc:  # noqa: BLE001
            return CanaryResult(False, [f"oracle error: {exc}"])


# ---------------------------------------------------------------------------
# Oracles: behavioral invariants, not string matching
# ---------------------------------------------------------------------------

def remediation_canary_oracle(ctx: dict, output: dict) -> CanaryResult:
    reasons: List[str] = []
    ok = True

    def fail(msg: str) -> None:
        nonlocal ok
        ok = False
        reasons.append(msg)

    remediation = (output.get("remediation") or "").lower().replace(" ", "")
    if "rollback" not in remediation:
        fail(f"expected a rollback recommendation, got: {output.get('remediation')!r}"[:80])
    if not output.get("evidence"):
        fail("expected diagnostic evidence, got none")
    actions = output.get("actions", [])
    if not any(a.get("kind") == "rollback" for a in actions):
        fail("expected a rollback action")
    for action in actions:
        if action.get("kind") == "restart" and action.get("target") == "database":
            fail("proposed the forbidden database restart")
    return CanaryResult(ok, reasons)


def _key_oracle(required_keys: List[str]):
    def oracle(ctx: dict, output: dict) -> CanaryResult:
        missing = [k for k in required_keys if not output.get(k)]
        if missing:
            return CanaryResult(False, [f"missing/empty keys: {missing}"])
        return CanaryResult(True, [])
    return oracle


_CANARY_INCIDENT = {"id": "CANARY-1", "service": "payments-api",
                    "symptom": "error rate > 5%", "severity": "P1"}
_CANARY_DIAGNOSIS = {"root_cause": "bad deployment v5.2",
                     "evidence": ["deploy log"], "confidence": 0.9}

# Deterministic oracles per role -- the ONE source of truth for pass/fail.
# Whoever generates a canary scenario, THIS judges it. Verdicts never come
# from a model.
ORACLES: Dict[str, Callable[[dict, dict], CanaryResult]] = {
    "triage": _key_oracle(["severity", "category"]),
    "diagnosis": _key_oracle(["root_cause", "evidence"]),
    "remediation": remediation_canary_oracle,
    "escalation": _key_oracle(["ticket_id", "summary"]),
}

_CANARY_CTX = {
    "triage": {"incident": dict(_CANARY_INCIDENT)},
    "diagnosis": {"incident": dict(_CANARY_INCIDENT)},
    "remediation": {"incident": dict(_CANARY_INCIDENT),
                    "diagnosis": dict(_CANARY_DIAGNOSIS)},
    "escalation": {"incident": dict(_CANARY_INCIDENT),
                   "remediation": {"remediation": "roll back"}},
}

CANARY_TASKS: Dict[str, CanaryTask] = {
    role: CanaryTask(
        name=f"{role}-canary-1", role=role,
        synthetic_ctx=dict(ctx),
        oracle=ORACLES[role],
    )
    for role, ctx in _CANARY_CTX.items()
}


# ---------------------------------------------------------------------------
# Canary generators: who writes the probes (a model may), who judges them
# (always the deterministic oracle above -- never a model).
# ---------------------------------------------------------------------------

class CanaryGenerator(Protocol):
    """Produces probe scenarios for a role. Never raises."""
    source: str  # "nemotron" | "static"

    def generate(self, role: str) -> List[CanaryTask]: ...


class StaticCanaryGenerator:
    """Deterministic fallback: the frozen per-role probe."""
    source = "static"

    def generate(self, role: str) -> List[CanaryTask]:
        return [CANARY_TASKS[role]]


class NemotronCanaryGenerator:
    """Nemotron invents diverse, adversarial probe scenarios per role.

    The model only *writes scenarios*. Every scenario is judged by the
    role's deterministic oracle from ORACLES -- the verdict path never
    touches a model. Any failure (no key, bad JSON, transport error)
    degrades silently to the static probe. Never raises.
    """

    def __init__(self, client=None, count: int = 3, config: dict = None,
                 logger=None, live: bool | None = None):
        from .nemotron import NemotronClient
        self._client = client or NemotronClient(config=config, logger=logger,
                                                live=live)
        self.count = count
        self._fallback = StaticCanaryGenerator()
        self.logger = getattr(self._client, "logger", None)

    @property
    def source(self) -> str:
        return "nemotron"

    @property
    def available(self) -> bool:
        return self._client.available

    def generate(self, role: str) -> List[CanaryTask]:
        try:
            return self._generate_inner(role)
        except Exception as exc:  # noqa: BLE001 - probes must never break the layer
            if self.logger is not None:
                self.logger.log("canary_generation_failed", level="WARNING",
                                role=role, error=str(exc))
            return self._fallback.generate(role)

    def _generate_inner(self, role: str) -> List[CanaryTask]:
        text = self._client.chat([
            {"role": "system",
             "content": ("You design adversarial test scenarios for AI "
                         "reliability engineering. Return ONLY a JSON array, "
                         "no prose, no markdown fences.")},
            {"role": "user", "content": self._prompt(role)},
        ], temperature=0.7, max_tokens=800)
        scenarios = _parse_scenarios(text)
        tasks = []
        for i, s in enumerate(scenarios[:self.count]):
            valid = _validate_scenario(s)
            if valid is None:
                if self.logger is not None:
                    self.logger.log("canary_scenario_rejected",
                                    level="WARNING", role=role,
                                    scenario=repr(s)[:120])
                continue
            incident = valid["incident"]
            ctx = {"incident": {"id": f"NEMOTRON-CANARY-{i + 1}", **incident}}
            if valid.get("diagnosis"):
                ctx["diagnosis"] = valid["diagnosis"]
            tasks.append(CanaryTask(
                name=valid["name"] or f"{role}-nemotron-canary-{i + 1}",
                role=role, synthetic_ctx=ctx, oracle=ORACLES[role]))
        if not tasks:
            raise ValueError("no valid scenarios in model output")
        return tasks

    def _prompt(self, role: str) -> str:
        return (
            f"Generate {self.count} adversarial canary probe scenarios for "
            f"testing whether the '{role}' agent in an incident-management "
            "workflow is still functioning. Each scenario should be a synthetic "
            "incident context cunningly designed to expose a degraded agent "
            "(one whose LLM is down, or producing plausible-but-wrong output). "
            "Vary services, symptoms, and severities.\n\n"
            "Return ONLY a JSON array of objects with keys: "
            '"name" (short id), "incident" (object with id/service/symptom/'
            'severity), and optionally "diagnosis" (object). Example:\n'
            '[{"name": "db-latency-spike", "incident": {"service": "orders-db", '
            '"symptom": "p99 latency 8s", "severity": "P1"}}]'
        )


def _validate_scenario(s) -> dict | None:
    """Schema validation for one model-generated canary scenario.

    A scenario is usable only if it is an object whose `incident` is an
    object with non-empty string service/symptom/severity. `name` and
    `diagnosis` are optional but must be the right types when present.
    Anything else is dropped (logged by the caller); if nothing valid
    remains, the generator falls back to the static probe. The model
    never gets to smuggle malformed context into an oracle run.
    """
    if not isinstance(s, dict):
        return None
    incident = s.get("incident")
    if not isinstance(incident, dict):
        return None
    for key in ("service", "symptom", "severity"):
        value = incident.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    name = s.get("name")
    if name is not None and not isinstance(name, str):
        return None
    diagnosis = s.get("diagnosis")
    if diagnosis is not None and not isinstance(diagnosis, dict):
        return None
    return {"name": (name or "").strip() or None,
            "incident": {k: incident[k] for k in ("service", "symptom",
                                                  "severity")},
            "diagnosis": diagnosis}


def _parse_scenarios(text: str) -> list:
    """Extract a JSON array from model output, tolerating fences/prose."""
    import json
    import re
    cleaned = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if m:
        cleaned = m.group(1).strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON array found in model output")
    parsed = json.loads(cleaned[start:end + 1])
    if not isinstance(parsed, list):
        raise ValueError("model output is not a JSON array")
    return parsed


def default_canary_generator(config: dict = None,
                            live: bool | None = None) -> CanaryGenerator:
    """Static probes unless live mode is EXPLICITLY enabled and the key is set.

    Credit-safety rule: the mere presence of NEBIUS_API_KEY never enables
    network calls. Pass live=True or set CONTINUITY_LIVE=1 for the Nemotron
    path (used by demo.py --live and the controlled live test only).
    """
    if live_enabled(live):
        gen = NemotronCanaryGenerator(config=config, live=live)
        if gen.available:
            return gen
    return StaticCanaryGenerator()

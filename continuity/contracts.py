"""Role contracts: machine-checkable health contracts per agent role.

A contract has human-readable must/must-not lists (for docs and the demo)
plus executable checks. Checks return a Violation or None.

Severity:
  anomaly  -> suspicious; feeds the hysteresis counter, triggers a canary
  critical -> business-invariant violation; immediate quarantine
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class Violation:
    severity: str  # "anomaly" | "critical"
    layer: str     # "contract" | "business"
    message: str


def _crit(layer: str, message: str) -> Violation:
    return Violation("critical", layer, message)


def _anom(layer: str, message: str) -> Violation:
    return Violation("anomaly", layer, message)


@dataclass
class RoleContract:
    role: str
    required_keys: List[str] = field(default_factory=list)
    must: List[str] = field(default_factory=list)
    must_not: List[str] = field(default_factory=list)
    checks: List[Callable[[dict, dict], Optional[Violation]]] = field(default_factory=list)

    def check(self, ctx: dict, output: dict) -> List[Violation]:
        violations: List[Violation] = []
        for key in self.required_keys:
            if key not in output:
                violations.append(_anom("contract", f"missing required key: {key}"))
        for check in self.checks:
            v = check(ctx, output)
            if v is not None:
                violations.append(v)
        return violations


# ---------------------------------------------------------------------------
# Shared check builders
# ---------------------------------------------------------------------------

def _value_in(field: str, allowed: set, layer: str = "contract"):
    def check(ctx: dict, output: dict) -> Optional[Violation]:
        if output.get(field) not in allowed:
            return _anom(layer, f"{field}={output.get(field)!r} not in {sorted(allowed)}")
        return None
    return check


# ---------------------------------------------------------------------------
# Business invariants (critical)
# ---------------------------------------------------------------------------

def no_database_restart(ctx: dict, output: dict) -> Optional[Violation]:
    for action in output.get("actions", []):
        if action.get("kind") == "restart" and action.get("target") == "database":
            return _crit("business",
                         "never restart the database for this incident class")
    return None


def remediation_needs_evidence(ctx: dict, output: dict) -> Optional[Violation]:
    if output.get("remediation") and not output.get("evidence"):
        return _crit("business",
                     "every remediation requires diagnostic evidence")
    return None


def rollback_needs_confirmed_deployment(ctx: dict, output: dict) -> Optional[Violation]:
    wants_rollback = any(a.get("kind") == "rollback" for a in output.get("actions", []))
    if wants_rollback:
        root_cause = ((ctx.get("diagnosis") or {}).get("root_cause") or "").lower()
        if "deploy" not in root_cause:
            return _crit("business",
                         "rollback requires a confirmed deployment root cause")
    return None


# ---------------------------------------------------------------------------
# Contract checks (anomaly)
# ---------------------------------------------------------------------------

def remediation_present(ctx: dict, output: dict) -> Optional[Violation]:
    if not output.get("remediation"):
        return _anom("contract",
                     "remediation is null/empty although status=success "
                     "(role is not fulfilling its contract)")
    return None


def diagnosis_has_evidence(ctx: dict, output: dict) -> Optional[Violation]:
    if not output.get("evidence"):
        return _anom("contract", "diagnosis produced no evidence")
    return None


def confidence_sane(ctx: dict, output: dict) -> Optional[Violation]:
    c = output.get("confidence")
    if not isinstance(c, (int, float)) or not (0.0 <= c <= 1.0):
        return _anom("contract", f"confidence {c!r} outside [0,1]")
    return None


# ---------------------------------------------------------------------------
# The four role contracts
# ---------------------------------------------------------------------------

TRIAGE_CONTRACT = RoleContract(
    role="triage",
    required_keys=["status", "severity", "category"],
    must=["assign a severity", "assign a category", "never drop the incident"],
    must_not=["invent incidents", "skip severity"],
    checks=[_value_in("severity", {"P1", "P2", "P3", "P4"})],
)

DIAGNOSIS_CONTRACT = RoleContract(
    role="diagnosis",
    required_keys=["status", "root_cause", "evidence", "confidence"],
    must=["identify a root cause", "attach evidence", "report confidence"],
    must_not=["diagnose without evidence"],
    checks=[diagnosis_has_evidence, confidence_sane],
)

REMEDIATION_CONTRACT = RoleContract(
    role="remediation",
    required_keys=["status", "remediation", "actions", "evidence"],
    must=["consume the diagnosis", "identify a remediation",
          "produce required evidence", "use approved remediation tools",
          "update incident state", "report completion"],
    must_not=["execute unauthorized actions", "omit required evidence",
              "return an empty remediation", "restart the database"],
    checks=[remediation_present,
            no_database_restart,
            remediation_needs_evidence,
            rollback_needs_confirmed_deployment],
)

ESCALATION_CONTRACT = RoleContract(
    role="escalation",
    required_keys=["status", "ticket_id", "summary"],
    must=["open a ticket", "summarize for the human"],
    must_not=["close the incident silently"],
)

CONTRACTS: Dict[str, RoleContract] = {
    "triage": TRIAGE_CONTRACT,
    "diagnosis": DIAGNOSIS_CONTRACT,
    "remediation": REMEDIATION_CONTRACT,
    "escalation": ESCALATION_CONTRACT,
}

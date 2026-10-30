"""Second framework-independence proof: a support-ticket pipeline.

Unlike examples/independent_booking.py (a provider with a book() call),
this pipeline's agents are plain objects with a run(ticket) method --
a different agent shape, three roles instead of one, and no external
effects at all (triage -> diagnosis -> escalation is pure data flow).
The pipeline module itself never imports continuity: everything the
layer needs lives in the wrap code below (shims, contracts, canary
tasks, standbys). The layer composes stages (the escalation summary
is enriched with the diagnosis root cause in the SHIM, not the agent).

Run:  python3 examples/independent_support.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from continuity.adapter import ContinuityLayer
from continuity.canary import CanaryResult, CanaryTask
from continuity.contracts import RoleContract
from continuity.standby import StandbyController


# --------------------------------------------------------------------------
# The untouched pipeline lives in examples/independent_support_pipeline.py
# (three run(ticket) agents, zero layer imports). Everything below is
# wrap code: shims, contracts, canaries, standbys.
# --------------------------------------------------------------------------

from examples.independent_support_pipeline import (
    DiagnosisBot,
    EscalationBot,
    TriageBot,
)


# --------------------------------------------------------------------------
# Wrap code (adapter side only): shims, contracts, canaries, standbys.
# --------------------------------------------------------------------------

class _RunShim:
    """Adapts a run(ticket) agent to the layer's handle(ctx) shape."""

    def __init__(self, agent_id: str, agent):
        self.agent_id = agent_id
        self.agent = agent

    def handle(self, ctx: dict) -> dict:
        return self.agent.run(ctx["ticket"])


class _EscalationShim(_RunShim):
    """Enriches the summary with the diagnosis, layer-side."""

    def handle(self, ctx: dict) -> dict:
        out = self.agent.run(ctx["ticket"])
        diagnosis = ctx.get("diagnosis") or {}
        if diagnosis.get("root_cause"):
            out = dict(out)
            out["summary"] = (f"{out['summary']} "
                              f"(root cause: {diagnosis['root_cause']})")
        return out


def _keys_oracle(required):
    def oracle(ctx: dict, output: dict) -> CanaryResult:
        missing = [k for k in required if not output.get(k)]
        if missing:
            return CanaryResult(False, [f"missing/empty keys: {missing}"])
        return CanaryResult(True, [])
    return oracle


def _contract(role: str, keys) -> RoleContract:
    return RoleContract(role=role, required_keys=list(keys),
                        must=[f"{role} output carries {', '.join(keys)}"],
                        must_not=[])


_CANARY_TICKET = {"id": "CANARY-9", "category": "payments",
                  "urgent": True}

_ROLE_SPEC = {
    "triage": (["severity", "category"], TriageBot),
    "diagnosis": (["root_cause", "evidence"], DiagnosisBot),
    "escalation": (["ticket_id", "summary"], EscalationBot),
}


def _make_layer(diagnosis_mode: str = "normal"):
    layer = ContinuityLayer()
    shims = {}
    for role, (keys, cls) in _ROLE_SPEC.items():
        if role == "diagnosis":
            primary_agent = cls(mode=diagnosis_mode)
            standby_agent = cls(mode="normal")
        else:
            primary_agent = cls()
            standby_agent = cls()
        shim_cls = _EscalationShim if role == "escalation" else _RunShim
        primary = shim_cls(f"support-{role}-primary", primary_agent)
        standby = StandbyController(
            shim_cls(f"support-{role}-standby", standby_agent))
        canary = CanaryTask(
            name=f"support-{role}-canary-1", role=role,
            synthetic_ctx={"ticket": dict(_CANARY_TICKET)},
            oracle=_keys_oracle(keys))
        layer.register(f"support-{role}", _contract(role, keys),
                       lambda output: {"output": output},
                       standby, primary=primary, canary_task=canary)
        shims[role] = primary
    return layer, shims


def protected_support_run(ticket: dict,
                          diagnosis_mode: str = "normal") -> dict:
    """Run triage -> diagnosis -> escalation under the layer."""
    layer, shims = _make_layer(diagnosis_mode)
    ctx = {"incident": {"id": f"TKT-{ticket['id']}"}, "ticket": ticket}
    triage = layer.invoke("support-triage", ctx, shims["triage"])
    ctx = dict(ctx, triage=triage["output"])
    diagnosis = layer.invoke("support-diagnosis", ctx, shims["diagnosis"])
    ctx = dict(ctx, diagnosis=diagnosis["output"])
    escalation = layer.invoke("support-escalation", ctx, shims["escalation"])
    return {"triage": triage["output"],
            "diagnosis": diagnosis["output"],
            "diagnosis_active": diagnosis["active"],
            "escalation": escalation["output"]}


def main() -> int:
    ticket = {"id": "1043", "category": "payments", "urgent": True}
    out = protected_support_run(ticket)
    print(f"normal: triage={out['triage']} diagnosis={out['diagnosis']} "
          f"escalation={out['escalation']}")
    out = protected_support_run(ticket, diagnosis_mode="degraded")
    print(f"degraded diagnosis: active={out['diagnosis_active']} "
          f"diagnosis={out['diagnosis']} "
          f"escalation={out['escalation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

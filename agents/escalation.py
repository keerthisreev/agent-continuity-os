"""Escalation agent: human recommendation + ticket. Plain logic."""
from __future__ import annotations
import json
from .base import IncidentAgent, ScriptedBackend


class EscalationAgent(IncidentAgent):
    def __init__(self, agent_id: str = "escalation", backend=None):
        super().__init__(agent_id, "escalation", backend or ScriptedBackend("{}"))

    def _run(self, ctx: dict) -> dict:
        inc_id = ctx["incident"]["id"]
        remediation = (ctx.get("remediation") or {}).get("remediation", "no remediation recorded")
        raw = self.backend.complete("ESCALATION " + inc_id)
        out = json.loads(raw) if raw.strip().startswith("{") else {}
        out.update({
            "status": "success",
            "ticket_id": f"TICK-{inc_id}",
            "summary": f"Human review: {remediation}",
        })
        return out

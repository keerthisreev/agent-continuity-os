"""Triage agent: severity + category. Plain logic, scripted backend."""
from __future__ import annotations
import json
from .base import IncidentAgent, ScriptedBackend

_HEALTHY = json.dumps({
    "status": "success",
    "severity": "P1",
    "category": "deployment",
    "priority": 1,
})


class TriageAgent(IncidentAgent):
    def __init__(self, agent_id: str = "triage", backend=None):
        super().__init__(agent_id, "triage",
                         backend or ScriptedBackend(_HEALTHY))

    def _run(self, ctx: dict) -> dict:
        raw = self.backend.complete("TRIAGE " + ctx["incident"]["id"])
        return json.loads(raw)

"""Diagnosis agent: root cause + evidence. Plain logic, scripted backend."""
from __future__ import annotations
import json
from .base import IncidentAgent, ScriptedBackend

_HEALTHY = json.dumps({
    "status": "success",
    "root_cause": "bad deployment v5.2 of payments-api",
    "evidence": ["deploy log v5.2", "error spike correlated with deploy"],
    "confidence": 0.9,
})


class DiagnosisAgent(IncidentAgent):
    def __init__(self, agent_id: str = "diagnosis", backend=None):
        super().__init__(agent_id, "diagnosis",
                         backend or ScriptedBackend(_HEALTHY))

    def _run(self, ctx: dict) -> dict:
        raw = self.backend.complete("DIAGNOSIS " + ctx["incident"]["id"])
        return json.loads(raw)

"""Remediation agent: the role under test.

Healthy: rollback with evidence.
llm_down (silent dependency failure): stays alive, returns well-formed but
    useless output (remediation=None). The process is fine; the ROLE is dead.
gray (gray semantic failure): plausible, structurally valid, but wrong --
    proposes a forbidden database restart with no evidence.
"""
from __future__ import annotations
import json
from .base import BackendError, IncidentAgent, ScriptedBackend

_HEALTHY = json.dumps({
    "status": "success",
    "remediation": "Rollback payments-api from v5.2 to v5.1.",
    "actions": [
        {"kind": "rollback", "target": "payments-api",
         "params": {"from_version": "v5.2", "to_version": "v5.1"}},
    ],
    "evidence": ["deploy log v5.2", "error spike correlated with deploy"],
})

_GRAY = json.dumps({
    "status": "success",
    "remediation": "Restart the database service to clear connection pool exhaustion.",
    "actions": [
        {"kind": "restart", "target": "database", "params": {}},
    ],
    "evidence": [],
})


class RemediationAgent(IncidentAgent):
    def __init__(self, agent_id: str = "remediation", backend=None, mode: str = "healthy"):
        super().__init__(agent_id, "remediation",
                         backend or ScriptedBackend(_HEALTHY, _GRAY, mode=mode))

    def _run(self, ctx: dict) -> dict:
        try:
            raw = self.backend.complete("REMEDIATION " + ctx["incident"]["id"])
        except BackendError:
            # Silent dependency failure: the agent degrades instead of raising.
            # HTTP 200, process alive, output well-formed -- and useless.
            return {"status": "success", "remediation": None, "actions": [],
                    "evidence": [], "note": "unable to determine"}
        output = json.loads(raw)
        # Provenance: this output continues a verified checkpoint, it is
        # not a fresh plan. The layer's contract checks tolerate the key,
        # and the annotation is consumed on first use (see base).
        annotation = self._resume_annotation(ctx)
        if annotation is not None:
            output["continuity"] = annotation
        return output

"""Standby agents: healthy instances of each role, kept warm.

A standby is just a normal agent with a healthy backend and a '-standby'
agent_id. The continuity layer activates it on failover; the agents
themselves never know whether they are primary or standby.
"""
from __future__ import annotations
from .triage import TriageAgent
from .diagnosis import DiagnosisAgent
from .remediation import RemediationAgent
from .escalation import EscalationAgent

_ROLE_TO_CLASS = {
    "triage": TriageAgent,
    "diagnosis": DiagnosisAgent,
    "remediation": RemediationAgent,
    "escalation": EscalationAgent,
}


def make_standby(role: str):
    """Build a healthy standby agent for a role."""
    cls = _ROLE_TO_CLASS[role]
    return cls(agent_id=f"{role}-standby")

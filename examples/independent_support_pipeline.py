"""Untouched support-ticket pipeline: three run(ticket) agents.

This module NEVER imports continuity. It is the pipeline the layer
wraps from outside (see examples/independent_support.py). Agents are
plain objects with a run(ticket) method -- a different agent shape
from the booking provider in examples/independent_booking.py.
"""


class TriageBot:
    """Severity + category for a ticket. Never fails in this demo."""

    def run(self, ticket: dict) -> dict:
        return {"severity": "P1" if ticket.get("urgent") else "P2",
                "category": ticket.get("category", "general")}


class DiagnosisBot:
    """Root cause + evidence. Degraded mode returns an empty dict --
    a silent failure: well-formed call, no usable output."""

    def __init__(self, mode: str = "normal"):
        self.mode = mode

    def run(self, ticket: dict) -> dict:
        if self.mode == "degraded":
            return {}
        return {"root_cause": "bad deployment v5.2",
                "evidence": ["deploy log"]}


class EscalationBot:
    """Ticket + summary for the on-call queue."""

    def run(self, ticket: dict) -> dict:
        return {"ticket_id": f"ESC-{ticket['id']}",
                "summary": "escalated to on-call"}

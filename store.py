"""Shared incident store (existing workflow infrastructure).

Not part of the continuity layer: this is the store the *existing*
workflow already writes to. Every write carries the writer role's current
fencing generation; the fencing authority rejects stale generations, and
duplicate action ids are refused (counted, not applied twice).

Thread-safe: safe under concurrent role invocations. The fencing check
runs INSIDE the store lock, atomically with the commit, so a generation
rotation that lands mid-authorization is observed before anything is
written -- a stale write cannot slip through between check and commit.
"""
from __future__ import annotations

import threading

from continuity.log import ContinuityLogger


class IncidentStore:
    def __init__(self, fencing, ledger, logger=None):
        self.fencing = fencing
        self.ledger = ledger
        self.logger = logger or getattr(ledger, "logger", None) or ContinuityLogger()
        self.applied: dict = {}   # action_id -> effect record
        self.incidents: dict = {}
        self._lock = threading.RLock()

    def apply_effect(self, agent_id: str, role: str, generation: int,
                     action_id: str, action: dict,
                     tick: int | None = None) -> bool:
        """Apply one action effect. Returns False if it was a duplicate.

        `action_id` is the explicit idempotency key: the same key is never
        applied twice, no matter which side (primary or standby) retries.
        `agent_id` is attribution (which physical agent wrote); `role` +
        `generation` is the fencing authority that must be current.
        """
        with self._lock:
            # Atomic with the commit below: a rotation landing while this
            # write is in flight is observed here, before the write lands.
            self.fencing.check(role, generation, tick)  # raises StaleEpochError
            if action_id in self.applied:
                self.logger.log("duplicate_refused", agent=agent_id, tick=tick,
                                idempotency_key=action_id)
                return False
            self.applied[action_id] = {
                "agent": agent_id, "role": role, "generation": generation,
                "kind": action.get("kind"), "target": action.get("target"),
                "params": action.get("params", {}),
            }
        self.logger.log("effect_applied", agent=agent_id, tick=tick,
                        idempotency_key=action_id,
                        kind=action.get("kind"), target=action.get("target"))
        return True

    def effects_for(self, incident_id: str):
        prefix = incident_id + ":"
        with self._lock:
            return {k: v for k, v in self.applied.items() if k.startswith(prefix)}

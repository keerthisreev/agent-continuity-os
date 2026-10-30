"""Shared per-role fencing generations: no split brain, ever.

One generation counter per logical ROLE -- not per agent. A role's primary
and its standby draw from the same counter, so rotating the generation on
failover or rejoin automatically invalidates whichever side was previously
authoritative. There is no separate "revoke the standby" step to forget:
after a rotation, only the side holding the new generation can write.

Every write to the incident store must carry the role's *current*
generation. A write carrying any older generation is REJECTED, whether it
comes from a dead primary, a stood-down standby, or a delayed in-flight
operation from before the rotation.

Optional generation TTL (``epoch.ttl_ticks`` in continuity.yaml, 0 = off):
when enabled, a generation that outlives its TTL is rejected like a stale
one. TTL is off by default, reproducing the brutal-proof behavior.

Thread-safe: safe under concurrent role invocations.
"""
from __future__ import annotations

import threading


class StaleEpochError(Exception):
    """Raised when a write carries a revoked, outdated, or expired generation.

    (Kept the historical name so existing imports keep working; it means
    "stale fencing generation".)
    """


class FencingAuthority:
    def __init__(self, ttl_ticks: int = 0, logger=None):
        self.ttl_ticks = ttl_ticks
        self.logger = logger
        self._gen: dict = {}          # role -> current generation
        self._issued_tick: dict = {}  # role -> tick of last rotation
        self._lock = threading.RLock()

    def _log(self, event, **kw):
        if self.logger is not None:
            self.logger.log(event, **kw)

    def rotate(self, role: str, tick: int | None = None) -> int:
        """Invalidate the role's current generation and issue the next one.

        The single fencing operation for failover AND rejoin: whoever held
        the old generation -- primary or standby -- can no longer write.
        Returns the new generation.
        """
        with self._lock:
            self._gen[role] = self._gen.get(role, 0) + 1
            if tick is not None:
                self._issued_tick[role] = tick
            gen = self._gen[role]
        self._log("generation_rotated", role=role, tick=tick, generation=gen)
        return gen

    def current(self, role: str) -> int:
        with self._lock:
            return self._gen.get(role, 0)

    def check(self, role: str, generation: int, tick: int | None = None) -> None:
        """Fail closed: outdated or expired generations are rejected.

        Call this atomically with the side effect it guards (the store
        holds its own lock across check+write) so a rotation landing
        mid-authorization is observed before anything commits.
        """
        with self._lock:
            current = self._gen.get(role, 0)
            expired = (
                self.ttl_ticks > 0
                and tick is not None
                and role in self._issued_tick
                and tick - self._issued_tick[role] > self.ttl_ticks
            )
        if generation != current or expired:
            reason = (
                "expired" if expired
                else f"stale (presented {generation}, current {current})"
            )
            self._log("fencing_rejection", role=role, tick=tick,
                      level="WARNING", generation=generation,
                      current=current, reason=reason)
            raise StaleEpochError(
                f"write rejected: role '{role}' presented generation "
                f"{generation}, current is {current} ({reason})"
            )

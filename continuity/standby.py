"""Standby controller: owns the standby agent lifecycle for one role.

The standby is an ordinary healthy agent. On activation it receives the
last known-good continuity package so it can RESUME mid-workflow instead
of restarting. It stands down on safe rejoin.

Fail-closed rule: a package whose schema version the standby cannot read
is LOUDLY rejected (UnknownSchemaVersion + error log) and never applied.
The caller (the adapter) then fails over with a fresh checkpoint instead
of resuming from unreadable state.
"""
from __future__ import annotations

import threading

from .checkpoint import UnknownSchemaVersion


class StandbyController:
    def __init__(self, standby_agent, logger=None):
        self.agent = standby_agent
        self.logger = logger
        self.active = False
        self.package = None
        self._lock = threading.RLock()

    def activate(self, package) -> None:
        """Take over for the failed primary, resuming from `package`.

        Raises UnknownSchemaVersion (fail closed) if the package schema
        is unreadable. `package` may be None (no checkpoint yet): the
        standby then starts the stage fresh.

        Restoration is real, not ceremonial: the package is handed to the
        standby agent's restore() hook so it resumes from the failed
        incident's verified state instead of re-running the stage blind.
        Agents without a restore() hook keep the old behavior (fresh
        execution + idempotent apply).
        """
        with self._lock:
            if package is not None:
                try:
                    package.check_version()
                except UnknownSchemaVersion as exc:
                    if self.logger is not None:
                        self.logger.log(
                            "standby_rejected_package",
                            level="ERROR",
                            agent=getattr(self.agent, "agent_id", "?"),
                            schema_version=getattr(package, "schema_version", "?"),
                            error=str(exc),
                        )
                    raise
            self.active = True
            self.package = package
            if package is not None:
                restore = getattr(self.agent, "restore", None)
                if callable(restore):
                    restore(package)

    def stand_down(self) -> None:
        with self._lock:
            self.active = False
            self.package = None

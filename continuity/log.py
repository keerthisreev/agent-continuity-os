"""Structured JSON-lines logging for the continuity layer.

Every ContinuityLogger keeps an in-memory record list (always on, hermetic
for tests) and optionally appends JSON lines to a file (set
``logging.path`` in continuity.yaml). Each entry carries the incident
correlation id, the logical tick, and the agent, so a failure can be
reconstructed from the log alone.
"""
from __future__ import annotations

import datetime
import json
import os
import threading


class ContinuityLogger:
    def __init__(self, name: str = "continuity", path: str | None = None):
        self.name = name
        self.path = path
        self.records: list = []
        self._lock = threading.RLock()
        self._fh = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")

    def log(self, event: str, *, tick=None, incident_id: str | None = None,
            agent: str | None = None, level: str = "INFO", **detail) -> dict:
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "logger": self.name,
            "level": level,
            "event": event,
            "tick": tick,
            "incident_id": incident_id,
            "agent": agent,
            "detail": detail,
        }
        line = json.dumps(entry, default=str)
        with self._lock:
            self.records.append(entry)
            if self._fh is not None:
                self._fh.write(line + "\n")
                self._fh.flush()
        return entry

    def events(self, kind: str) -> list:
        """All recorded entries for one event kind."""
        with self._lock:
            return [r for r in self.records if r["event"] == kind]

    def to_jsonl(self, path: str) -> int:
        """Export the in-memory records as JSON lines. Returns count."""
        with self._lock:
            lines = [json.dumps(r, default=str) for r in self.records]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        return len(lines)

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

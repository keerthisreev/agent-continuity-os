"""Forensic ledger: the complete, timestamped record of every failure.

Records the failure window (first anomaly -> confirmed -> failover ->
recovery -> rejoin), the affected-work map (which incidents entered the
window and each item's disposition), duplicate writes prevented, and the
full ordered timeline. Timestamps are logical ticks: deterministic.

Every entry is also mirrored to the structured ContinuityLogger with the
incident correlation id, so the JSON-lines log alone can reconstruct a
failure.

Thread-safe: safe under concurrent role invocations.
"""
from __future__ import annotations

import json
import threading
from typing import Dict, List, Optional

from .metrics import compute_metrics  # noqa: F401  (re-exported for convenience)


class ForensicLedger:
    def __init__(self, clock, logger=None):
        self.clock = clock
        self.logger = logger
        self.timeline: List[dict] = []
        self.windows: List[dict] = []
        self.stage_records: List[dict] = []
        self.duplicates_prevented: int = 0
        self.human_review: List[dict] = []
        self._lock = threading.RLock()

    # -- primitives ------------------------------------------------------
    def record(self, kind: str, detail: dict, tick: Optional[int] = None) -> None:
        t = self.clock.now() if tick is None else tick
        entry = {"tick": t, "kind": kind, "detail": detail}
        with self._lock:
            self.timeline.append(entry)
        if self.logger is not None:
            incident = detail.get("incident") if isinstance(detail, dict) else None
            agent = detail.get("agent") if isinstance(detail, dict) else None
            self.logger.log(kind, tick=t, incident_id=incident, agent=agent,
                            detail=detail)

    def record_stage(self, incident_id: str, stage: str, agent_id: str,
                     tick: int, outcome: str) -> None:
        """outcome: ok | bad_output | crashed | standby"""
        with self._lock:
            self.stage_records.append({
                "incident": incident_id, "stage": stage, "agent": agent_id,
                "tick": tick, "outcome": outcome,
            })
        self.record("stage", {"incident": incident_id, "stage": stage,
                              "agent": agent_id, "outcome": outcome}, tick)

    def flag_human_review(self, incident_id: str, reason: str,
                          tick: Optional[int] = None) -> None:
        t = self.clock.now() if tick is None else tick
        with self._lock:
            self.human_review.append({"incident": incident_id, "reason": reason, "tick": t})
        self.record("human_review_flagged",
                    {"incident": incident_id, "reason": reason}, t)

    def count_duplicate_prevented(self, action_id: str,
                                  tick: Optional[int] = None) -> None:
        with self._lock:
            self.duplicates_prevented += 1
            count = self.duplicates_prevented
        self.record("duplicate_write_prevented",
                    {"action_id": action_id, "total_prevented": count}, tick)

    # -- failure windows ---------------------------------------------------
    def begin_failure_window(self, agent_id: str, tick: int, reason: str,
                             first_anomaly_tick: int) -> None:
        with self._lock:
            self.windows.append({
                "agent": agent_id,
                "first_anomaly_tick": first_anomaly_tick,
                "quarantined_tick": tick,
                "failover_tick": None,
                "end_tick": None,
                "reason": reason,
                "resolution": None,
            })
        self.record("failure_window_open",
                    {"agent": agent_id, "reason": reason,
                     "first_anomaly_tick": first_anomaly_tick}, tick)

    def mark_failover(self, tick: int, standby_id: str, standby_generation: int) -> None:
        with self._lock:
            if self.windows:
                self.windows[-1]["failover_tick"] = tick
        self.record("failover",
                    {"standby": standby_id,
                     "standby_generation": standby_generation}, tick)

    def end_failure_window(self, tick: int, resolution: str) -> None:
        with self._lock:
            if self.windows:
                self.windows[-1]["end_tick"] = tick
                self.windows[-1]["resolution"] = resolution
        self.record("failure_window_closed", {"resolution": resolution}, tick)

    # -- derived views -------------------------------------------------------
    def _incidents_in_window(self, start: int, end: int) -> List[str]:
        with self._lock:
            records = list(self.stage_records)
        seen = []
        for rec in records:
            if start <= rec["tick"] <= end and rec["incident"] not in seen:
                seen.append(rec["incident"])
        return seen

    def _disposition(self, incident_id: str) -> str:
        with self._lock:
            recs = [r for r in self.stage_records if r["incident"] == incident_id]
            flagged = any(h["incident"] == incident_id for h in self.human_review)
        outcomes = [r["outcome"] for r in recs]
        if flagged:
            return "human review"
        if any(o in ("bad_output", "crashed") for o in outcomes):
            if "standby" in outcomes:
                return "replayed by standby"
            return "failed - needs replay"
        if "standby" in outcomes:
            return "recovered via standby"
        if recs and all(o == "ok" for o in outcomes):
            return "completed clean"
        return "incomplete"

    def affected_work(self) -> Dict[str, str]:
        """incident_id -> disposition for every incident the layer has seen."""
        with self._lock:
            seen = []
            for rec in self.stage_records:
                if rec["incident"] not in seen:
                    seen.append(rec["incident"])
        return {inc_id: self._disposition(inc_id) for inc_id in seen}

    def window_incidents(self) -> List[str]:
        """Incidents with stage activity inside the latest failure window."""
        with self._lock:
            windows = list(self.windows)
        if not windows:
            return []
        w = windows[-1]
        start = w["first_anomaly_tick"]
        end = w["end_tick"] or w["failover_tick"] or start
        return self._incidents_in_window(start, end)

    def tsfd(self) -> Optional[int]:
        """Time to Semantic Failure Detection: first anomaly -> failover."""
        with self._lock:
            windows = list(self.windows)
        if not windows:
            return None
        w = windows[-1]
        if w["failover_tick"] is None:
            return None
        return w["failover_tick"] - w["first_anomaly_tick"]

    def diagnostician_notes(self) -> List[dict]:
        with self._lock:
            return [{"tick": e["tick"], "agent": e["detail"].get("agent"),
                     "note": e["detail"].get("note")}
                    for e in self.timeline if e["kind"] == "diagnostician_note"]

    def report(self) -> dict:
        # NOTE: key set is frozen -- test_ledger_completeness asserts it.
        return {
            "failure_windows": self.windows,
            "affected_work": self.affected_work(),
            "window_incidents": self.window_incidents(),
            "stage_records": self.stage_records,
            "duplicates_prevented": self.duplicates_prevented,
            "human_review": self.human_review,
            "tsfd_ticks": self.tsfd(),
            "timeline": self.timeline,
        }

    # -- forensic export -----------------------------------------------------
    def export_dict(self) -> dict:
        """Full forensic report: windows, affected work with dispositions,
        ordered timeline, TSFD/TRV/AFR, duplicate counter, diagnostician
        notes. Superset of report()."""
        affected = self.affected_work()
        with self._lock:
            windows = [dict(w) for w in self.windows]
            stage_records = list(self.stage_records)
            human_review = list(self.human_review)
            duplicates = self.duplicates_prevented
            timeline = sorted(self.timeline, key=lambda e: e["tick"])
        return {
            "schema": "agent-continuity/forensic-report@1",
            "failure_windows": windows,
            "affected_work": affected,   # incident_id -> disposition
            "window_incidents": self.window_incidents(),
            "stage_records": stage_records,
            "duplicates_prevented": duplicates,
            "human_review": human_review,
            "metrics": compute_metrics(self),
            "diagnostician_notes": self.diagnostician_notes(),
            "timeline": timeline,
        }

    def export_json(self, path: str) -> str:
        """Write the full forensic report as JSON. Returns the path."""
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.export_dict(), fh, indent=2, default=str)
        if self.logger is not None:
            self.logger.log("forensic_export", path=path)
        return path

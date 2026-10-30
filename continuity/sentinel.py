"""The undercover Sentinel: deterministic role-health watchdog.

Health is a VECTOR, not a score: liveness / contract / capability /
business. Hysteresis keeps one bad output from causing a flapping
failover:

    HEALTHY --anomaly--> DEGRADED --(canary fail | repeated anomalies)--> QUARANTINED
        ^                      |                                            |
        |--- clean streak -----+                                     FAILOVER
                                                                             |
        HEALTHY <-- REJOIN <-- probes pass <-- RECOVERING <-- primary heals --+

Canaries are suspicion-triggered (never continuous): they run when an
anomaly is observed or during recovery probing. A failed canary, or any
critical business-invariant violation, quarantines the agent.

The optional diagnostician only annotates the ledger; it never changes a
verdict. With diagnostician=None the sentinel is fully authoritative.
"""
from __future__ import annotations
from dataclasses import dataclass
import functools
import threading
from typing import Dict, List, Optional

from .config import get as _cfg_get


def _locked(method):
    """Serialize per-agent observation through the sentinel's RLock."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
QUARANTINED = "QUARANTINED"
FAILOVER = "FAILOVER"
RECOVERING = "RECOVERING"
REJOIN = "REJOIN"

_ACTIVE_STATES = (QUARANTINED, FAILOVER, RECOVERING)  # standby is authoritative


@dataclass
class HealthVector:
    liveness: Optional[bool] = None
    contract: Optional[bool] = None
    capability: Optional[bool] = None
    business: Optional[bool] = None

    def as_dict(self) -> dict:
        return {"liveness": self.liveness, "contract": self.contract,
                "capability": self.capability, "business": self.business}


class Sentinel:
    def __init__(self, *, fencing, ledger, canary_runner, clock,
                 diagnostician=None, anomaly_threshold: int = None,
                 clean_streak_to_heal: int = None, config: dict = None,
                 logger=None):
        self.fencing = fencing
        self.ledger = ledger
        self.canary_runner = canary_runner
        self.clock = clock
        self.diagnostician = diagnostician
        cfg = config or {}

        def _opt(explicit, *keys, fallback):
            if explicit is not None:
                return explicit
            return _cfg_get(cfg, *keys, default=fallback)

        self.anomaly_threshold = _opt(anomaly_threshold, "hysteresis",
                                      "anomaly_threshold", fallback=2)
        self.clean_streak_to_heal = _opt(clean_streak_to_heal, "hysteresis",
                                         "clean_streak_to_heal", fallback=2)
        self.logger = logger
        self._lock = threading.RLock()
        self._reg: Dict[str, dict] = {}
        self.health: Dict[str, HealthVector] = {}
        self.state: Dict[str, str] = {}
        self.anomalies: Dict[str, int] = {}
        self.clean_streak: Dict[str, int] = {}
        self.first_anomaly_tick: Dict[str, int] = {}
        self.last_violations: Dict[str, List[str]] = {}

    # -- registration ----------------------------------------------------
    def register(self, agent_id: str, contract, canary_tasks, primary_agent) -> None:
        # canary_tasks: list of CanaryTask (a lone task is wrapped).
        if not isinstance(canary_tasks, (list, tuple)):
            canary_tasks = [canary_tasks]
        with self._lock:
            self._reg[agent_id] = {"contract": contract,
                                   "canary_tasks": list(canary_tasks),
                                   "primary": primary_agent}
            self.health[agent_id] = HealthVector()
            self.state[agent_id] = HEALTHY
            self.anomalies[agent_id] = 0
            self.clean_streak[agent_id] = 0
            self.last_violations[agent_id] = []

    def state_of(self, agent_id: str) -> str:
        with self._lock:
            return self.state[agent_id]

    def active_for(self, agent_id: str) -> str:
        """Which side is authoritative: 'primary' or 'standby'."""
        with self._lock:
            state = self.state[agent_id]
        return "standby" if state in _ACTIVE_STATES else "primary"

    def primary_of(self, agent_id: str):
        with self._lock:
            return self._reg[agent_id]["primary"]

    def violations_of(self, agent_id: str) -> List:
        """Thread-safe copy of the last violations for an agent."""
        with self._lock:
            return list(self.last_violations.get(agent_id, []))

    def set_state(self, agent_id: str, new_state: str, tick: int) -> None:
        with self._lock:
            old = self.state[agent_id]
            if old == new_state:
                return
            self.state[agent_id] = new_state
        self.ledger.record("state_transition",
                           {"agent": agent_id, "from": old, "to": new_state}, tick)
        if self.logger is not None:
            self.logger.log("state_transition", tick=tick, agent=agent_id,
                            **{"from": old, "to": new_state})

    # -- canary suites ---------------------------------------------------
    def _run_canary_suite(self, agent_id: str, tick: int,
                          kind: str) -> "CanaryResult":
        """Run every registered probe for the role. One ledger event per
        probe (kind preserved, task name added); the suite fails if ANY
        probe fails. The oracle judging each probe is deterministic --
        whoever generated the scenario, a model never judges."""
        from .canary import CanaryResult
        reg = self._reg[agent_id]
        reasons: List[str] = []
        failed = False
        for task in reg["canary_tasks"]:
            result = self.canary_runner.run(reg["primary"], task)
            self.ledger.record(
                kind,
                {"agent": agent_id, "task": task.name,
                 "passed": result.passed, "reasons": result.reasons}, tick)
            if not result.passed:
                failed = True
                reasons.extend(f"[{task.name}] {r}" for r in result.reasons)
        return CanaryResult(not failed, reasons)

    # -- observation -------------------------------------------------------
    def _consult(self, agent_id: str, tick: int) -> None:
        """Optional reasoning note. Advisory only -- never alters the verdict."""
        if self.diagnostician is None:
            return
        hv = self.health[agent_id]
        note = self.diagnostician.diagnose(
            agent_id, hv.as_dict(),
            [v.message for v in self.last_violations[agent_id]])
        self.ledger.record("diagnostician_note", {"agent": agent_id, "note": note}, tick)

    @_locked
    def check_output(self, agent_id: str, ctx: dict, output: dict,
                     tick: int) -> str:
        """Evaluate one agent output. Returns 'ok' | 'degraded' | 'failover'."""
        reg = self._reg[agent_id]
        hv = self.health[agent_id]
        hv.liveness = True
        violations = reg["contract"].check(ctx, output)
        self.last_violations[agent_id] = violations
        crit = [v for v in violations if v.severity == "critical"]
        anom = [v for v in violations if v.severity == "anomaly"]

        if crit:
            hv.business = False
            if anom:
                hv.contract = False
            self.ledger.record(
                "critical_violation",
                {"agent": agent_id,
                 "violations": [v.message for v in crit]}, tick)
            # Targeted canary for evidence, then quarantine regardless.
            result = self._run_canary_suite(agent_id, tick, "targeted_canary")
            hv.capability = result.passed
            self._consult(agent_id, tick)
            self._quarantine(agent_id, tick,
                             "critical business-invariant violation")
            return "failover"

        if anom:
            hv.contract = False
            self.anomalies[agent_id] += 1
            self.clean_streak[agent_id] = 0
            if agent_id not in self.first_anomaly_tick:
                self.first_anomaly_tick[agent_id] = tick
            self.ledger.record(
                "anomaly",
                {"agent": agent_id,
                 "violations": [v.message for v in anom],
                 "anomaly_count": self.anomalies[agent_id]}, tick)
            if self.anomalies[agent_id] == 1:
                self.set_state(agent_id, DEGRADED, tick)
            # Suspicion-triggered canary (never continuous).
            result = self._run_canary_suite(agent_id, tick, "suspicion_canary")
            hv.capability = result.passed
            self._consult(agent_id, tick)
            if not result.passed or self.anomalies[agent_id] >= self.anomaly_threshold:
                reason = (f"canary failed: {result.reasons}"
                          if not result.passed else "repeated anomalies")
                self._quarantine(agent_id, tick, reason)
                return "failover"
            return "degraded"

        hv.contract = True
        hv.business = True
        self.clean_streak[agent_id] += 1
        if (self.state[agent_id] == DEGRADED
                and self.clean_streak[agent_id] >= self.clean_streak_to_heal):
            self.state[agent_id] = HEALTHY
            self.anomalies[agent_id] = 0
            self.ledger.record("returned_to_healthy", {"agent": agent_id}, tick)
        return "ok"

    @_locked
    def report_crash(self, agent_id: str, tick: int, exc: Exception) -> str:
        """Hard failure: the process is gone. No hysteresis needed."""
        if self.state[agent_id] in _ACTIVE_STATES:
            self.ledger.record("crash_while_failed_over",
                               {"agent": agent_id, "error": str(exc)}, tick)
            return "failover"
        self.health[agent_id].liveness = False
        if agent_id not in self.first_anomaly_tick:
            self.first_anomaly_tick[agent_id] = tick
        self.ledger.record("crash", {"agent": agent_id, "error": str(exc)}, tick)
        self._consult(agent_id, tick)
        self._quarantine(agent_id, tick, f"process dead: {exc}")
        return "failover"

    def _quarantine(self, agent_id: str, tick: int, reason: str) -> None:
        self.set_state(agent_id, QUARANTINED, tick)
        self.ledger.begin_failure_window(
            agent_id, tick, reason,
            first_anomaly_tick=self.first_anomaly_tick.get(agent_id, tick))

    # -- recovery ------------------------------------------------------------
    @_locked
    def probe_recovery(self, agent_id: str, tick: int) -> bool:
        """Run recovery probes against the primary. True if it may rejoin."""
        reg = self._reg[agent_id]
        primary = reg["primary"]
        if not primary.alive:
            self.ledger.record("recovery_probe",
                               {"agent": agent_id, "passed": False,
                                "reasons": ["process still dead"]}, tick)
            return False
        result = self._run_canary_suite(agent_id, tick, "recovery_probe")
        return result.passed

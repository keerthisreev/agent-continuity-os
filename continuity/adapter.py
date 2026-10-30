"""The adapter boundary: the continuity layer underneath existing agents.

Usage:

    layer = ContinuityLayer()                      # diagnostician optional
    layer.register(agent_id="remediation",
                   role_contract=REMEDIATION_CONTRACT,
                   state_adapter=remediation_state_adapter,
                   standby=StandbyController(make_standby("remediation")),
                   primary=remediation_agent)

    result = layer.invoke("remediation", ctx, remediation_agent)
    layer.apply_effects(result, store)

Agents are never modified. The layer observes inputs/outputs, checkpoints
healthy outputs, routes to the standby on failover, and fences shared per-role generations.
"""
from __future__ import annotations

import threading

from .canary import CANARY_TASKS, CanaryRunner, default_canary_generator
from .checkpoint import STATE_ADAPTERS, UnknownSchemaVersion, capture_package
from .config import CONFIG, get as _cfg_get
from .diagnostician import Diagnostician
from .fencing import FencingAuthority, StaleEpochError
from .ledger import ForensicLedger
from .log import ContinuityLogger
from .nemotron import live_enabled
from .sentinel import (FAILOVER, HEALTHY, RECOVERING, REJOIN, Sentinel)


class Clock:
    """Logical clock: deterministic ticks, no wall time. Thread-safe."""

    def __init__(self):
        self.t = 0
        self._lock = threading.RLock()

    def tick(self) -> int:
        with self._lock:
            self.t += 1
            return self.t

    def now(self) -> int:
        with self._lock:
            return self.t


class ContinuityLayer:
    def __init__(self, diagnostician: Diagnostician = None, config: dict = None,
                 canary_generator=None, live: bool | None = None):
        # Credit-safety: live Nebius calls require explicit opt-in
        # (live=True or CONTINUITY_LIVE=1). Key presence alone never suffices.
        self.live = live_enabled(live)
        self.config = config if config is not None else CONFIG
        self.logger = ContinuityLogger(
            path=_cfg_get(self.config, "logging", "path", default=None))
        self.clock = Clock()
        self.fencing = FencingAuthority(
            ttl_ticks=_cfg_get(self.config, "epoch", "ttl_ticks", default=0),
            logger=self.logger)
        self.ledger = ForensicLedger(self.clock, logger=self.logger)
        self.canary_runner = CanaryRunner()
        # Who writes the probes: Nemotron only with explicit live opt-in
        # (scenarios only), the frozen static probe otherwise. Whoever
        # writes them, the deterministic oracle judges them.
        self.canary_generator = (canary_generator
                                 or default_canary_generator(self.config,
                                                             live=self.live))
        self.sentinel = Sentinel(
            fencing=self.fencing, ledger=self.ledger,
            canary_runner=self.canary_runner, clock=self.clock,
            diagnostician=diagnostician, config=self.config,
            logger=self.logger)
        self._reg: dict = {}
        # (agent_id, incident_id) -> ContinuityPackage. One slot per
        # incident: a failover can never hand the standby another
        # incident's checkpoint.
        self._checkpoints: dict = {}
        self._lock = threading.RLock()

    #: Bound on retained checkpoints; oldest tick evicted past it.
    _CHECKPOINT_CAP = 500

    def _store_checkpoint(self, key, package) -> None:
        self._checkpoints[key] = package
        if len(self._checkpoints) > self._CHECKPOINT_CAP:
            oldest = min(self._checkpoints,
                         key=lambda k: self._checkpoints[k].tick)
            del self._checkpoints[oldest]

    def checkpoint_package(self, agent_id: str, incident_id: str):
        """Return the continuity package for (agent, incident), or None.

        Read-only accessor so reporting surfaces (sim, ablation,
        dashboard) never reach into _checkpoints directly.
        """
        with self._lock:
            return self._checkpoints.get((agent_id, incident_id))

    # -- registration ------------------------------------------------------
    def register(self, agent_id: str, role_contract, state_adapter,
                 standby, primary=None, canary_task=None,
                 canary_tasks=None) -> None:
        role = role_contract.role
        if canary_tasks is not None:
            tasks = list(canary_tasks)
        elif canary_task is not None:
            tasks = [canary_task]
        else:
            tasks = self.canary_generator.generate(role) or [CANARY_TASKS[role]]
        adapter = state_adapter or STATE_ADAPTERS[role]
        self._reg[agent_id] = {
            "role": role, "contract": role_contract,
            "state_adapter": adapter, "standby": standby,
            "canary_tasks": tasks,
        }
        # The standby belongs to the layer's observability boundary now.
        if getattr(standby, "logger", None) is None:
            standby.logger = self.logger
        self.sentinel.register(agent_id, role_contract, tasks, primary)
        generation = self.fencing.rotate(role)
        self.ledger.record("registered",
                           {"agent": agent_id, "role": role,
                            "generation": generation})
        self.ledger.record("canary_probes_generated",
                           {"agent": agent_id, "role": role,
                            "source": self.canary_generator.source,
                            "count": len(tasks),
                            "names": [t.name for t in tasks]})

    # -- invocation ----------------------------------------------------------
    @staticmethod
    def _action_id(incident_id: str, role: str, index: int, action: dict) -> str:
        return f"{incident_id}:{role}:{index}:{action.get('kind')}:{action.get('target')}"

    def invoke(self, agent_id: str, ctx: dict, agent_fn):
        """Invoke the authoritative side (primary or standby) for a role.

        Returns a result dict the caller passes to apply_effects().
        On failover the stage is transparently retried with the standby.

        Thread-safe: the layer lock guards observation/failover/checkpoint
        sections; the agent's own handle() runs outside it so concurrent
        roles are not serialized behind slow agents. Component locks
        (sentinel, fencing, ledger, store, clock) protect shared state.

        Checkpoint slots are keyed by (agent, incident): concurrent
        incidents can never corrupt each other's resume decisions, and a
        failover always receives the failed incident's own package.
        """
        reg = self._reg[agent_id]
        role = reg["role"]
        incident_id = ctx["incident"]["id"]
        for _ in range(2):  # at most one failover per invocation
            with self._lock:
                active = self.sentinel.active_for(agent_id)
                agent = agent_fn if active == "primary" else reg["standby"].agent
                generation = self.fencing.current(role)
                tick = self.clock.tick()
                self.ledger.record(
                    "invoke",
                    {"agent": agent.agent_id, "role": role,
                     "incident": incident_id, "generation": generation}, tick)
            try:
                output = agent.handle(ctx)
            except Exception as exc:  # noqa: BLE001 - crash is a signal
                with self._lock:
                    self.sentinel.report_crash(agent_id, tick, exc)
                    self.ledger.record_stage(incident_id, role, agent.agent_id,
                                             tick, "crashed")
                    if active == "standby":
                        raise RuntimeError(
                            f"standby {agent.agent_id} also crashed")
                    self._do_failover(agent_id, role, incident_id, tick)
                continue
            with self._lock:
                decision = self.sentinel.check_output(agent_id, ctx, output, tick)
                if decision == "failover":
                    self.ledger.record_stage(incident_id, role, agent.agent_id,
                                             tick, "bad_output")
                    crit = [v for v in self.sentinel.violations_of(agent_id)
                            if v.severity == "critical"]
                    if crit:
                        # A plausible-but-wrong output was *attempted*: a human
                        # should review what almost went out.
                        self.ledger.flag_human_review(
                            incident_id,
                            f"{agent_id} attempted: {crit[0].message}", tick)
                    self._do_failover(agent_id, role, incident_id, tick)
                    continue
                outcome = "standby" if active == "standby" else "ok"
                self.ledger.record_stage(incident_id, role, agent.agent_id,
                                         tick, outcome)
                if decision == "ok":
                    planned = [self._action_id(incident_id, role, i, a)
                               for i, a in enumerate(output.get("actions", []))]
                    new_pkg = capture_package(
                        agent_id, role, incident_id, output, tick,
                        reg["state_adapter"], planned)
                    # Preserve already-completed work for this incident across
                    # the failover boundary: the standby must not redo it.
                    # The slot is incident-scoped, so the merge can only ever
                    # see this incident's own history.
                    key = (agent_id, incident_id)
                    prev = self._checkpoints.get(key)
                    if prev is not None:
                        assert prev.incident_id == incident_id
                        for aid in prev.completed_action_ids:
                            if (aid in new_pkg.planned_action_ids
                                    and aid not in new_pkg.completed_action_ids):
                                new_pkg.completed_action_ids.append(aid)
                    self._store_checkpoint(key, new_pkg)
                return {"agent_id": agent_id, "role": role,
                        "incident_id": incident_id, "active": active,
                        "active_agent_id": agent.agent_id,
                        "generation": generation,
                        "output": output, "tick": tick}
        raise RuntimeError(f"failover loop for {agent_id}")

    def _do_failover(self, agent_id: str, role: str, incident_id: str,
                     tick: int) -> None:
        reg = self._reg[agent_id]
        # The failed incident's OWN checkpoint -- never another incident's.
        package = self._checkpoints.get((agent_id, incident_id))
        if package is not None:
            assert package.incident_id == incident_id, (
                f"checkpoint incident mismatch: slot {(agent_id, incident_id)} "
                f"holds {package.incident_id}")
        # One shared generation per role: rotating it invalidates the failed
        # primary AND any previously-authorized standby in a single step.
        generation = self.fencing.rotate(role, tick)
        self.ledger.record("fencing",
                           {"role": role, "action": "generation rotated",
                            "generation": generation}, tick)
        standby = reg["standby"]
        try:
            standby.activate(package)
        except UnknownSchemaVersion:
            # Fail closed: never resume from an unreadable package. The
            # standby takes over with a fresh checkpoint instead.
            self.ledger.record(
                "continuity_package_rejected",
                {"agent": agent_id,
                 "schema_version": getattr(package, "schema_version", None),
                 "fallback": "standby starts fresh"}, tick)
            self.logger.log("continuity_package_rejected", level="ERROR",
                            tick=tick, agent=agent_id,
                            schema_version=getattr(package, "schema_version", None))
            standby.activate(None)
            package = None
        self.sentinel.set_state(agent_id, FAILOVER, tick)
        self.ledger.mark_failover(tick, standby.agent.agent_id, generation)
        self.ledger.record(
            "standby_activated",
            {"standby": standby.agent.agent_id, "generation": generation,
             "resumed_from_tick": package.tick if package else None,
             "resumed_from_incident": package.incident_id if package else None},
            tick)

    # -- effects -------------------------------------------------------------
    def apply_effects(self, result: dict, store) -> None:
        """Apply an output's actions to the store: idempotent, fenced.

        Every action carries an explicit idempotency key; skips are logged
        with their reason. Thread-safe.
        """
        with self._lock:
            agent_id = result["agent_id"]
            package = self._checkpoints.get((agent_id, result["incident_id"]))
            for i, action in enumerate(result["output"].get("actions", [])):
                idempotency_key = self._action_id(result["incident_id"],
                                                 result["role"], i, action)
                if package and idempotency_key in package.completed_action_ids:
                    # Standby replays the plan; already-done work is skipped.
                    self._skip_duplicate(result, idempotency_key,
                                         "already completed in continuity package")
                    continue
                try:
                    applied = store.apply_effect(result["active_agent_id"],
                                                 result["role"],
                                                 result["generation"],
                                                 idempotency_key,
                                                 action, tick=result["tick"])
                except StaleEpochError as exc:
                    # Lost the race with a failover: the write is rejected,
                    # never half-applied. Record and move on.
                    self.ledger.record("stale_write_rejected",
                                       {"agent": result["active_agent_id"],
                                        "idempotency_key": idempotency_key,
                                        "reason": str(exc)}, result["tick"])
                    continue
                if not applied:
                    self._skip_duplicate(result, idempotency_key,
                                         "already applied in store")
                    continue
                if (package
                        and idempotency_key not in package.completed_action_ids):
                    package.completed_action_ids.append(idempotency_key)

    def _skip_duplicate(self, result: dict, idempotency_key: str,
                        reason: str) -> None:
        self.ledger.count_duplicate_prevented(idempotency_key, result["tick"])
        self.logger.log("idempotent_skip", tick=result["tick"],
                        incident_id=result["incident_id"],
                        agent=result["active_agent_id"],
                        idempotency_key=idempotency_key, reason=reason)

    # -- recovery --------------------------------------------------------------
    def attempt_rejoin(self, agent_id: str) -> bool:
        """Revalidate the primary and return it to service at a new generation.

        The standby stays authoritative until every probe passes.
        Thread-safe.
        """
        with self._lock:
            return self._attempt_rejoin_locked(agent_id)

    def _attempt_rejoin_locked(self, agent_id: str) -> bool:
        tick = self.clock.tick()
        if self.sentinel.state_of(agent_id) != FAILOVER:
            return False
        self.sentinel.set_state(agent_id, RECOVERING, tick)
        if not self.sentinel.probe_recovery(agent_id, tick):
            self.sentinel.set_state(agent_id, FAILOVER, tick)
            self.ledger.record("rejoin_denied", {"agent": agent_id}, tick)
            return False
        # Shared per-role generation: rotating it hands write authority to
        # the recovered primary AND invalidates the standby's generation in
        # the same step. A delayed standby write after this point is a
        # stale write, and the store will reject it.
        role = self._reg[agent_id]["role"]
        new_generation = self.fencing.rotate(role, tick)
        self.sentinel.set_state(agent_id, REJOIN, tick)
        self._reg[agent_id]["standby"].stand_down()
        self.sentinel.set_state(agent_id, HEALTHY, tick)
        self.ledger.end_failure_window(
            tick, f"primary rejoined at generation {new_generation}")
        self.ledger.record("rejoin_complete",
                           {"agent": agent_id,
                            "new_generation": new_generation}, tick)
        return True

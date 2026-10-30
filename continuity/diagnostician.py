"""Optional reasoning component -- the Nemotron seam.

The deterministic Sentinel is ALWAYS authoritative. The diagnostician only
attaches explanatory notes to the forensic ledger; it can never change a
verdict. The sentinel reaches correct verdicts with the diagnostician
disabled (None).

Two Nemotron jobs (both advisory, both visible in the demo):
  1. diagnose() -- per-anomaly explanatory notes (existing behavior)
  2. incident_narrative() -- a 2-3 sentence forensic summary for the human
     reviewer, written from the failure window + affected-work map

When live mode is EXPLICITLY enabled (live=True or CONTINUITY_LIVE=1)
and the NEBIUS_API_KEY environment variable is set, both go through the
Nebius Token Factory OpenAI-compatible chat-completions endpoint. Otherwise
— all tests, all default demos, the dashboard — the deterministic stub
answers. Key presence alone never enables network calls: a credit must not
be spent by accident. No network call is ever made without explicit live
opt-in.

Reliability contract:
  - diagnose()/incident_narrative() NEVER raise: every failure degrades to
    stub text with a log line
  - 20s timeout, 2 retries with exponential backoff, circuit breaker
    (3 consecutive failures -> 60s open -> silent stub fallback)
"""
from __future__ import annotations

from typing import List, Protocol

from .nemotron import NemotronClient, live_enabled


class Diagnostician(Protocol):
    def diagnose(self, agent_id: str, health_vector: dict,
                 recent_violations: List[str]) -> str: ...

    def incident_narrative(self, report: dict) -> str: ...


class StubDiagnostician:
    """Deterministic fallback. No network, no model, no surprises."""

    def diagnose(self, agent_id: str, health_vector: dict,
                 recent_violations: List[str]) -> str:
        unhealthy = [k for k, v in health_vector.items() if v is False]
        return ("stub-diagnosis(%s): deterministic verdict stands; "
                "failing layers=%s; violations=%d; no model consulted"
                % (agent_id, unhealthy or "none", len(recent_violations)))

    def incident_narrative(self, report: dict) -> str:
        return _stub_narrative(report)


def _stub_narrative(report: dict) -> str:
    """Deterministic forensic summary. Advisory only, never a verdict."""
    windows = report.get("failure_windows") or []
    w = windows[0] if windows else {}
    affected = report.get("affected_work") or {}
    affected_txt = (", ".join(f"{k} ({v})" for k, v in affected.items())
                    or "none recorded")
    return (
        "stub-narrative (deterministic, advisory only): role '%s' was fenced "
        "and failed over at tick %s after first anomaly at tick %s (%s). "
        "Affected work: %s. Duplicate writes prevented: %s."
        % (w.get("agent", "?"), w.get("failover_tick", "?"),
           w.get("first_anomaly_tick", "?"), w.get("reason", "?"),
           affected_txt, report.get("duplicates_prevented", "?")))


class NemotronDiagnostician:
    """Reasoning assistant via Nebius Token Factory (OpenAI-compatible).

    Secrets come from the environment ONLY (NEBIUS_API_KEY). Everything
    else comes from continuity.yaml / constructor args.
    """

    def __init__(self, api_key: str = "", base_url: str = "",
                 model: str = "", timeout_s: float = None,
                 retries: int = None, circuit_breaker_failures: int = None,
                 circuit_breaker_open_s: float = None,
                 backoff_base_s: float = 0.5,
                 transport=None, config: dict = None, logger=None,
                 live: bool | None = None):
        self._client = NemotronClient(
            api_key=api_key, base_url=base_url, model=model,
            timeout_s=timeout_s, retries=retries,
            circuit_breaker_failures=circuit_breaker_failures,
            circuit_breaker_open_s=circuit_breaker_open_s,
            backoff_base_s=backoff_base_s, transport=transport,
            config=config, logger=logger, live=live)
        self.logger = self._client.logger

    @property
    def available(self) -> bool:
        return self._client.available

    @property
    def circuit_open(self) -> bool:
        return self._client.circuit_open

    @property
    def model(self) -> str:
        return self._client.model

    # -- public ----------------------------------------------------------
    def diagnose(self, agent_id: str, health_vector: dict,
                 recent_violations: List[str]) -> str:
        """Advisory note. NEVER raises: degrades to stub text on any failure."""
        try:
            text = self._client.chat([
                {"role": "system",
                 "content": "You are a terse reliability engineer. Two sentences max."},
                {"role": "user", "content": self._prompt(
                    agent_id, health_vector, recent_violations)},
            ], temperature=0.2, max_tokens=300)
            return (f"nemotron-diagnosis({agent_id}) [{self.model}]: "
                    f"{text[:600]}")
        except Exception as exc:  # noqa: BLE001 - never break the sentinel
            self.logger.log("diagnostician_failed", level="WARNING",
                            agent=agent_id, error=str(exc))
            return StubDiagnostician().diagnose(agent_id, health_vector,
                                                recent_violations)

    def incident_narrative(self, report: dict) -> str:
        """2-3 sentence forensic summary for the human reviewer.

        Advisory only: written from the confirmed ledger, never consulted
        for a verdict. NEVER raises.
        """
        try:
            text = self._client.chat([
                {"role": "system",
                 "content": ("You are a terse reliability engineer writing a "
                             "forensic incident summary. 2-3 sentences max.")},
                {"role": "user", "content": self._narrative_prompt(report)},
            ], temperature=0.2, max_tokens=300)
            return (f"nemotron-narrative [{self.model}] (advisory only): {text[:600]}")
        except Exception as exc:  # noqa: BLE001 - never break the sentinel
            self.logger.log("narrative_failed", level="WARNING",
                            error=str(exc))
            return _stub_narrative(report)

    # -- internals ---------------------------------------------------------
    def _prompt(self, agent_id, health_vector, recent_violations) -> str:
        import json
        return (
            "You are a reliability engineer watching an AI incident-management "
            "agent. A deterministic watchdog already decided the agent's health; "
            "your job is to explain it in one or two sentences for the incident "
            "record. Do not hedge about the verdict.\n\n"
            f"Agent: {agent_id}\n"
            f"Health vector: {json.dumps(health_vector)}\n"
            f"Recent violations: {recent_violations}\n\n"
            "Explain what failed and what it means operationally."
        )

    def _narrative_prompt(self, report: dict) -> str:
        import json
        windows = report.get("failure_windows") or []
        w = windows[0] if windows else {}
        brief = {
            "failed_role": w.get("agent"),
            "first_anomaly_tick": w.get("first_anomaly_tick"),
            "failover_tick": w.get("failover_tick"),
            "reason": w.get("reason"),
            "affected_work": report.get("affected_work"),
            "duplicates_prevented": report.get("duplicates_prevented"),
            "human_review": [
                {"incident": h.get("incident"), "reason": h.get("reason")}
                for h in (report.get("human_review") or [])],
            "recovery": ("primary rejoined at new generation"
                         if any(e.get("kind") == "rejoin_complete"
                                for e in (report.get("timeline") or []))
                         else "standby remained authoritative"),
        }
        return (
            "A deterministic watchdog already confirmed this incident and its "
            "handling; write a 2-3 sentence forensic summary for the human "
            "reviewer covering: what failed, what work was affected and how it "
            "was handled, and the recovery outcome. Do not hedge about the "
            "verdict; it is confirmed.\n\n"
            f"Incident facts: {json.dumps(brief, default=str)}"
        )


def default_diagnostician(config: dict = None,
                          live: bool | None = None) -> Diagnostician:
    """Nemotron via Nebius only with explicit live opt-in AND the key set.

    Otherwise the deterministic stub. See live_enabled() for the rule:
    key presence alone never spends credit.
    """
    if live_enabled(live):
        candidate = NemotronDiagnostician(config=config, live=live)
        if candidate.available:
            return candidate
    return StubDiagnostician()

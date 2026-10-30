"""Continuity metrics, computed from the forensic ledger.

TSFD  Time to Semantic Failure Detection: ticks from the first observed
      anomaly to the confirmed failover. Measures how fast the layer
      catches a role that *looks* healthy.
TRV   Time to Verified Recovery: ticks from failover to the end of the
      failure window (verified recovery + rejoin). Measures how fast
      trustworthy operation is restored.
AFR   Affected Work Recovery Rate: fraction of affected incidents whose
      disposition shows the work was recovered (completed clean, replayed
      or recovered via standby) over all affected incidents.

All ticks are logical (deterministic). None means "not applicable".
"""
from __future__ import annotations

RECOVERED_DISPOSITIONS = frozenset({
    "completed clean",
    "replayed by standby",
    "recovered via standby",
})


def tsfd(ledger) -> int | None:
    """Ticks from first anomaly to confirmed failover (latest window)."""
    windows = list(getattr(ledger, "windows", []) or [])
    if not windows:
        return None
    w = windows[-1]
    if w.get("failover_tick") is None:
        return None
    return w["failover_tick"] - w["first_anomaly_tick"]


def trv(ledger) -> int | None:
    """Ticks from failover to verified recovery (window end)."""
    windows = list(getattr(ledger, "windows", []) or [])
    if not windows:
        return None
    w = windows[-1]
    if w.get("failover_tick") is None or w.get("end_tick") is None:
        return None
    return w["end_tick"] - w["failover_tick"]


def afr(ledger) -> float | None:
    """Affected Work Recovery Rate over every incident the layer has seen."""
    affected = ledger.affected_work()
    if not affected:
        return None
    recovered = sum(1 for d in affected.values() if d in RECOVERED_DISPOSITIONS)
    return recovered / len(affected)


def compute_metrics(ledger) -> dict:
    return {
        "tsfd_ticks": tsfd(ledger),
        "trv_ticks": trv(ledger),
        "afr": afr(ledger),
    }

"""Compact reliability scorecard, rendered from the real ledger.

Folded into the recovery report (chat "what happened?" answer and the
ablation comparison): TSFD / TRV / AFR from continuity/metrics.py,
duplicates prevented, the affected-work buckets, and the exact command
that reruns the scenario. Nothing here is hard-coded; every number is
read off the ledger of the run being reported on.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import ROLES

# Disposition strings are the ledger's own vocabulary (continuity/ledger.py).
BUCKET_COMPLETED = "completed clean"
BUCKET_RECOVERED = ("recovered via standby", "replayed by standby")
BUCKET_REVIEW = "human review"


def bucketize(affected_work: Dict[str, str]) -> Dict[str, List[str]]:
    """Split incident_id -> disposition into the three headline buckets."""
    buckets: Dict[str, List[str]] = {
        "completed_clean": [],
        "recovered_via_standby": [],
        "needs_human_review": [],
    }
    for inc_id, disposition in affected_work.items():
        if disposition == BUCKET_COMPLETED:
            buckets["completed_clean"].append(inc_id)
        elif disposition in BUCKET_RECOVERED:
            buckets["recovered_via_standby"].append(inc_id)
        elif disposition == BUCKET_REVIEW:
            buckets["needs_human_review"].append(inc_id)
        # "incomplete" / "failed - needs replay" are transient states of a
        # session still in flight; the report only claims finished buckets.
    return buckets


def build_scorecard(layer, rerun_cmd: str) -> Dict[str, Any]:
    """Build the scorecard from a live ContinuityLayer. `rerun_cmd` is the
    exact shell command that reproduces the scenario being scored."""
    from continuity import metrics as m

    computed = m.compute_metrics(layer.ledger)
    report = layer.ledger.export_dict()
    affected = report.get("affected_work") or {}
    return {
        "tsfd_ticks": computed.get("tsfd_ticks"),
        "trv_ticks": computed.get("trv_ticks"),
        "afr": computed.get("afr"),
        "has_failure_window": bool(layer.ledger.windows),
        "duplicates_prevented": report.get("duplicates_prevented", 0),
        "buckets": bucketize(affected),
        "fencing_generations": {
            role: layer.fencing.current(role) for role in ROLES
        },
        "rerun": rerun_cmd,
    }


def render_text(card: Dict[str, Any]) -> List[str]:
    """Plain-text rendering of the scorecard for chat/CLI surfaces."""
    b = card["buckets"]
    afr = card["afr"]
    tsfd = card["tsfd_ticks"]
    trv = card["trv_ticks"]
    has_window = card.get("has_failure_window", True)
    if tsfd is None:
        tsfd_str = "n/a (no failover in this run)"
    else:
        tsfd_str = (f"{tsfd} ticks"
                    + (" (caught in the same observation cycle)"
                       if tsfd == 0 else ""))
    if trv is None:
        trv_str = ("- (failure window still open)" if has_window
                   else "- (no failure window in this run)")
    else:
        trv_str = f"{trv} ticks"
    lines = [
        "RELIABILITY SCORECARD (from the ledger, not a success message)",
        f"  detection (TSFD) ............ {tsfd_str}",
        f"  verified recovery (TRV) ..... {trv_str}",
        f"  recovery rate (AFR) ......... "
        f"{('%.0f%%' % (afr * 100)) if afr is not None else '-'}",
        f"  duplicate writes prevented .. {card['duplicates_prevented']}",
        "  affected work:",
        f"    completed clean ........... {', '.join(b['completed_clean']) or '-'}",
        f"    recovered via standby ..... {', '.join(b['recovered_via_standby']) or '-'}",
        f"    needs human review ........ {', '.join(b['needs_human_review']) or '-'}",
        f"  fencing generations ......... {card['fencing_generations']}",
        f"  rerun ....................... {card['rerun']}",
    ]
    return lines

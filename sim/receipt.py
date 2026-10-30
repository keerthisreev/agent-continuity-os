"""Recovery receipt: proof of recovery, from real records only.

The receipt answers "is it safe to call this complete?" Every field is
read off the operation journal, the forensic ledger, the store, or the
scorecard -- never hard-coded. Missing data renders as "not recorded",
never as an invented success.

Per-operation: original operation, effect receipt, retry decision,
recovery result. Global: checkpoint resumed-from, fencing decision,
final verified state.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

NOT_RECORDED = "not recorded"

# Journal recovery_decision -> receipt retry-decision vocabulary.
_DECISION_TO_RETRY = {
    "reuse_committed_receipt": "reused",
    "reconciled_ambiguous_commit__receipt_reused": "reused",
    "no_recorded_effect__verify_provider_before_retry": "needs-review",
    "receipt_from_journal__provider_unverifiable__needs_review":
        "needs-review",
}


def _decision_source(record: Optional[dict]) -> str:
    if record is None:
        return NOT_RECORDED
    d = record.get("recovery_decision")
    return f"journal.recovery_decision={d}" if d else (
        f"journal.status={record.get('status')}")


def build_receipt(incident_id: str, operations: List[Dict[str, Any]],
                  layer=None) -> Dict[str, Any]:
    """Assemble the receipt.

    operations: list of dicts, each with:
      op_id, label, kind, target,
      effect_receipt (dict or None),
      retry_decision (str, caller-derived from records),
      decision_source (str, which record established it),
      recovery_result (str, per-step result sentence, caller-derived)
    layer: a ContinuityLayer (optional) -- supplies checkpoint, fencing,
      and final-state fields from its ledger. Absent -> "not recorded".
    """
    ops = []
    for op in operations:
        ops.append({
            "op_id": op.get("op_id") or NOT_RECORDED,
            "label": op.get("label") or NOT_RECORDED,
            "kind": op.get("kind") or NOT_RECORDED,
            "target": op.get("target") or NOT_RECORDED,
            "effect_receipt": op.get("effect_receipt") or NOT_RECORDED,
            "retry_decision": op.get("retry_decision") or NOT_RECORDED,
            "decision_source": op.get("decision_source") or NOT_RECORDED,
            "recovery_result": op.get("recovery_result") or NOT_RECORDED,
        })

    checkpoint = NOT_RECORDED
    fencing = NOT_RECORDED
    final_state = NOT_RECORDED
    if layer is not None:
        checkpoint = _checkpoint_field(layer)
        fencing = _fencing_field(layer)
        final_state = _final_state_field(layer, incident_id)

    return {
        "incident_id": incident_id,
        "operations": ops,
        "checkpoint": checkpoint,
        "fencing": fencing,
        "final_state": final_state,
    }


def _checkpoint_field(layer) -> Any:
    resume = next(
        ({"standby": (e.get("detail") or {}).get("standby"),
          "generation": (e.get("detail") or {}).get("generation"),
          "resumed_from_tick": (e.get("detail") or {}).get("resumed_from_tick"),
          "resumed_from_incident":
              (e.get("detail") or {}).get("resumed_from_incident")}
         for e in layer.ledger.timeline
         if e.get("kind") == "standby_activated"),
        None)
    if resume is None:
        return ("no failover in this run -- recovery did not need a "
                "checkpoint resume")
    return resume


def _fencing_field(layer) -> Dict[str, Any]:
    rejected = [
        {"tick": e.get("tick"),
         "agent": (e.get("detail") or {}).get("agent"),
         "idempotency_key": (e.get("detail") or {}).get("idempotency_key"),
         "reason": (e.get("detail") or {}).get("reason")}
        for e in layer.ledger.timeline
        if e.get("kind") == "stale_write_rejected"
    ]
    from . import ROLES
    generations = {role: layer.fencing.current(role) for role in ROLES}
    if not rejected:
        return {
            "stale_writes_rejected": 0,
            "note": ("no stale writes attempted; every write was authorized "
                     "at the role's current fencing generation"),
            "current_generations": generations,
        }
    return {
        "stale_writes_rejected": len(rejected),
        "rejections": rejected,
        "current_generations": generations,
    }


def _final_state_field(layer, incident_id: str) -> Dict[str, Any]:
    disposition = layer.ledger.affected_work().get(incident_id, NOT_RECORDED)
    return {
        "disposition": disposition,
        "duplicates_prevented": layer.ledger.duplicates_prevented,
        "human_review_flags": [
            h.get("reason") for h in layer.ledger.human_review
            if h.get("incident") == incident_id
        ],
    }


# -- assemblers: scenario results -> receipt operations ----------------------

def receipt_from_lost_ack(result: Dict[str, Any]) -> Dict[str, Any]:
    """Full journal receipt for the lost-ack protected run."""
    journal = result["journal"]
    operations = []
    for s in result["steps"]:
        rec = journal.get(s["op_id"])
        decision = _DECISION_TO_RETRY.get(
            (rec or {}).get("recovery_decision") or "",
            "committed first try (no retry needed)"
            if rec and rec["status"] in ("committed", "reconciled")
            else NOT_RECORDED)
        if s.get("reused"):
            if s.get("needs_review"):
                recovery_result = ("ack lost after commit; the journal holds "
                                   "a receipt but the provider cannot confirm "
                                   "it -- needs human review; nothing was "
                                   "blindly re-booked")
            else:
                recovery_result = ("ack lost after commit; journal reconciled; "
                                   "existing receipt reused -- no duplicate "
                                   "effect")
        else:
            recovery_result = "completed on first attempt"
        operations.append({
            "op_id": s["op_id"],
            "label": s["step"]["label"],
            "kind": s["step"]["kind"],
            "target": s["step"]["target"],
            "effect_receipt": s["receipt"],
            "retry_decision": decision,
            "decision_source": _decision_source(rec),
            "recovery_result": recovery_result,
        })
    return build_receipt(result["incident_id"], operations,
                         layer=result["layer"])


def receipt_from_blocked_action(incident_id: str, protected: Dict[str, Any],
                                label: str, decision_source: str,
                                recovery_result: str) -> Dict[str, Any]:
    """Receipt for challenges where the layer blocked the action (unsafe
    proposal, stale write): there is no journal record -- the proof is the
    ledger + fencing, and the receipt says exactly that."""
    blocked = protected.get("blocked_actions") or [label]
    operations = [{
        "op_id": f"{incident_id}:blocked:{i}",
        "label": f"{a} (proposed, never executed)",
        "kind": a.split(":")[0] if ":" in a else a,
        "target": a.split(":")[1] if ":" in a else NOT_RECORDED,
        "effect_receipt": NOT_RECORDED,
        "retry_decision": "blocked",
        "decision_source": decision_source,
        "recovery_result": recovery_result,
    } for i, a in enumerate(blocked)]
    return build_receipt(incident_id, operations,
                         layer=protected.get("layer"))


def render_text_lines(receipt: Dict[str, Any]) -> List[str]:
    """Plain-text rendering for chat/CLI/test surfaces."""
    lines = [f"RECOVERY RECEIPT -- incident {receipt['incident_id']} "
             f"(from journal + ledger, not a success message)"]
    for op in receipt["operations"]:
        lines.append(f"  operation .... {op['label']} [{op['op_id']}]")
        r = op["effect_receipt"]
        lines.append(f"    effect receipt : "
                     f"{r if isinstance(r, str) else _one_line(r)}")
        lines.append(f"    retry decision : {op['retry_decision']} "
                     f"({op['decision_source']})")
        lines.append(f"    recovery result: {op['recovery_result']}")
    lines.append(f"  checkpoint ... {_one_line(receipt['checkpoint'])}")
    lines.append(f"  fencing ...... {_one_line(receipt['fencing'])}")
    lines.append(f"  final state .. {_one_line(receipt['final_state'])}")
    return lines


def _one_line(value: Any) -> str:
    import json as _json
    if isinstance(value, str):
        return value
    try:
        return _json.dumps(value, default=str)
    except Exception:  # noqa: BLE001
        return str(value)

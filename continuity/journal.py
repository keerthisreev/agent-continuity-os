"""SQLite-backed operation journal: reconcile-before-retry discipline.

The journal sits between the agent and the downstream effect. Before an
agent retries an operation whose outcome is uncertain (the acknowledgement
was lost), it consults the journal instead of blindly re-executing:

  record_intent -> record_commit -> (ack lost) mark_unknown -> reconcile
                                                              -> reuse receipt

Honesty boundary (read this before citing the journal as a guarantee):

  Exactly-once effects against ARBITRARY external services cannot be had
  from an ID alone. The journal provides reconcile-before-retry
  discipline; the guarantee additionally requires EITHER
  (a) the downstream provider to honor idempotency keys (the simulated
      provider in sim/lost_ack.py does), OR
  (b) a reliable way to re-read the provider's state before retrying.
  Without (a) or (b), the journal can tell you what YOU recorded, not
  what the provider did. The sim is explicit about which one it uses.

Crash safety: WAL mode, synchronous=NORMAL, busy_timeout, one explicit
commit per state transition. Tests prove records survive close/reopen.

Statuses: pending -> committed -> unknown -> reconciled.
  pending     intent recorded, effect not yet confirmed
  committed   effect confirmed, receipt on file
  unknown     acknowledgement lost; the agent cannot tell what happened
  reconciled  the journal re-checked: receipt reused or provider re-check
              ordered (see recovery_decision)

Conflict rule: the same idempotency key with a DIFFERENT canonical
operation is a conflict -- it is rejected loudly, never silently
treated as the same operation. Likewise, a key already associated with
a DIFFERENT logical operation id is rejected: keys are never shared
across operations.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

STATUSES = ("pending", "committed", "unknown", "reconciled")

# recovery_decision vocabulary written by reconcile().
DECISION_REUSE_COMMITTED = "reuse_committed_receipt"
DECISION_RECONCILED_AMBIGUOUS = "reconciled_ambiguous_commit__receipt_reused"
DECISION_NO_RECORD = "no_recorded_effect__verify_provider_before_retry"
# Written by mark_needs_review(): the journal holds a receipt the provider
# cannot confirm. Presented as needs-review, never as a verified recovery.
DECISION_UNVERIFIED_NEEDS_REVIEW = \
    "receipt_from_journal__provider_unverifiable__needs_review"


def _canonical_operation(service: Optional[str], op: Optional[str],
                         target: Optional[str],
                         params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The full canonical operation identity. Two operations are the same
    only if service, operation type, target, AND params all match."""
    return {"service": service, "op": op, "target": target,
            "params": params if params is not None else {}}


def _fingerprint(operation: Dict[str, Any]) -> str:
    """Canonical operation fingerprint: sha256 over sorted-key JSON of the
    COMPLETE operation (service, operation type, target, params). The same
    key + params for a different target or op type is a different
    operation and fingerprints differently."""
    canonical = json.dumps(operation if operation is not None else {},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "journal.db")


class IdempotencyConflict(Exception):
    """Same idempotency key, different operation: refuse, don't silently reuse."""

    def __init__(self, logical_op_id: str, idempotency_key: str,
                 expected_fingerprint: str, presented_fingerprint: str):
        self.logical_op_id = logical_op_id
        self.idempotency_key = idempotency_key
        self.expected_fingerprint = expected_fingerprint
        self.presented_fingerprint = presented_fingerprint
        super().__init__(
            f"idempotency conflict for key {idempotency_key!r}: the same key "
            f"was presented with a different operation (op {logical_op_id!r}). "
            f"Refusing to treat them as the same operation.")


class IdempotencyKeyMismatch(IdempotencyConflict):
    """The idempotency key is already associated with a DIFFERENT logical
    operation. Subclass of IdempotencyConflict so existing handlers catch
    it; the message names both operations."""

    def __init__(self, logical_op_id: str, idempotency_key: str,
                 existing_op_id: str):
        self.logical_op_id = logical_op_id
        self.idempotency_key = idempotency_key
        self.existing_op_id = existing_op_id
        # Bypass IdempotencyConflict.__init__ (its fingerprint args don't
        # apply here); call Exception directly.
        super(IdempotencyConflict, self).__init__(
            f"idempotency key {idempotency_key!r} is already associated with "
            f"operation {existing_op_id!r}, not {logical_op_id!r}. Keys are "
            f"never shared across operations: refusing.")


class OperationJournal:
    """Durable per-operation record with reconcile-before-retry semantics."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or _default_path()
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS operations (
                     logical_op_id     TEXT PRIMARY KEY,
                     idempotency_key   TEXT UNIQUE NOT NULL,
                     payload_fingerprint TEXT NOT NULL,
                     status            TEXT NOT NULL,
                     receipt           TEXT,
                     fencing_generation INTEGER,
                     recovery_decision TEXT,
                     created_at        TEXT NOT NULL,
                     updated_at        TEXT NOT NULL
                   )""")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_operations_key "
                "ON operations(idempotency_key)")
            self._conn.commit()

    # -- internals -------------------------------------------------------
    def _row_to_record(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["receipt"] = json.loads(d["receipt"]) if d["receipt"] else None
        return d

    def _get_by_key_locked(self, key: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT * FROM operations WHERE idempotency_key = ?", (key,))
        row = cur.fetchone()
        return self._row_to_record(row) if row else None

    # -- public API ------------------------------------------------------
    def get(self, logical_op_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM operations WHERE logical_op_id = ?",
                (logical_op_id,))
            row = cur.fetchone()
            return self._row_to_record(row) if row else None

    def all_records(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM operations ORDER BY created_at, logical_op_id")
            return [self._row_to_record(r) for r in cur.fetchall()]

    def record_intent(self, logical_op_id: str, idempotency_key: str,
                      service: Optional[str] = None,
                      op: Optional[str] = None,
                      target: Optional[str] = None,
                      params: Optional[Dict[str, Any]] = None,
                      fencing_generation: Optional[int] = None) -> Dict[str, Any]:
        """Register intent to perform an operation.

        The operation identity is the FULL canonical operation
        (service, op type, target, params): two intents are the same only
        if all four match.

        REUSE path: the same key for the same logical operation with the
        same fingerprint returns the existing record (committed/reconciled
        -> reuse its receipt; pending/unknown -> the caller must reconcile
        first). No duplicate intent row is ever created.
        CONFLICT path: the same key with a different fingerprint raises
        IdempotencyConflict; the same key for a different logical
        operation raises IdempotencyKeyMismatch -- never silently treated
        as identical.
        """
        canon = _canonical_operation(service, op, target, params)
        fp = _fingerprint(canon)
        now = _now()
        with self._lock:
            existing = self._get_by_key_locked(idempotency_key)
            if existing is not None:
                if existing["logical_op_id"] != logical_op_id:
                    raise IdempotencyKeyMismatch(
                        logical_op_id, idempotency_key,
                        existing["logical_op_id"])
                if existing["payload_fingerprint"] != fp:
                    raise IdempotencyConflict(
                        logical_op_id, idempotency_key,
                        existing["payload_fingerprint"], fp)
                return existing
            by_op = self.get(logical_op_id)
            if by_op is not None and by_op["idempotency_key"] != idempotency_key:
                raise IdempotencyConflict(
                    logical_op_id, idempotency_key,
                    by_op["payload_fingerprint"], fp)
            if by_op is not None:
                return by_op
            self._conn.execute(
                """INSERT INTO operations
                   (logical_op_id, idempotency_key, payload_fingerprint,
                    status, receipt, fencing_generation, recovery_decision,
                    created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, ?)""",
                (logical_op_id, idempotency_key, fp, fencing_generation,
                 now, now))
            self._conn.commit()
            return self.get(logical_op_id)

    def record_commit(self, logical_op_id: str,
                      receipt: Dict[str, Any]) -> Dict[str, Any]:
        """The effect is confirmed: store the receipt, mark committed."""
        now = _now()
        with self._lock:
            if self.get(logical_op_id) is None:
                raise KeyError(f"no such operation: {logical_op_id}")
            self._conn.execute(
                "UPDATE operations SET status='committed', receipt=?, "
                "updated_at=? WHERE logical_op_id = ?",
                (json.dumps(receipt), now, logical_op_id))
            self._conn.commit()
            return self.get(logical_op_id)

    def mark_unknown(self, logical_op_id: str) -> Dict[str, Any]:
        """The acknowledgement was lost: the agent can no longer tell
        whether the effect happened. The receipt (if any) is kept -- the
        journal still knows what it recorded; the AGENT does not."""
        now = _now()
        with self._lock:
            if self.get(logical_op_id) is None:
                raise KeyError(f"no such operation: {logical_op_id}")
            self._conn.execute(
                "UPDATE operations SET status='unknown', updated_at=? "
                "WHERE logical_op_id = ?",
                (now, logical_op_id))
            self._conn.commit()
            return self.get(logical_op_id)

    def reconcile(self, logical_op_id: str) -> Dict[str, Any]:
        """Re-check the journal before retrying. Returns the record; the
        caller reuses record['receipt'] instead of re-executing when one
        is on file. The decision is recorded, not just returned."""
        now = _now()
        with self._lock:
            rec = self.get(logical_op_id)
            if rec is None:
                raise KeyError(f"no such operation: {logical_op_id}")
            status, receipt = rec["status"], rec["receipt"]
            if status == "reconciled":
                return rec  # idempotent: the decision stands
            if receipt is not None:
                decision = (DECISION_REUSE_COMMITTED if status == "committed"
                            else DECISION_RECONCILED_AMBIGUOUS)
                self._conn.execute(
                    "UPDATE operations SET status='reconciled', "
                    "recovery_decision=?, updated_at=? "
                    "WHERE logical_op_id = ?",
                    (decision, now, logical_op_id))
            else:
                # Honest limit: nothing on file. The agent must verify
                # provider state before retrying -- never retry blind.
                decision = DECISION_NO_RECORD
                self._conn.execute(
                    "UPDATE operations SET recovery_decision=?, updated_at=? "
                    "WHERE logical_op_id = ?",
                    (decision, now, logical_op_id))
            self._conn.commit()
            return self.get(logical_op_id)

    def mark_needs_review(self, logical_op_id: str,
                          decision: str) -> Dict[str, Any]:
        """The journal holds a receipt the provider cannot confirm: record
        needs-review instead of presenting a verified recovery. The caller
        (the gateway) established that re-verification failed; the journal
        records the outcome, it does not invent success."""
        now = _now()
        with self._lock:
            if self.get(logical_op_id) is None:
                raise KeyError(f"no such operation: {logical_op_id}")
            self._conn.execute(
                "UPDATE operations SET recovery_decision=?, updated_at=? "
                "WHERE logical_op_id = ?",
                (decision, now, logical_op_id))
            self._conn.commit()
            return self.get(logical_op_id)

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

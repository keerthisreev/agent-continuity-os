"""Model-usage tracking and spending guardrail for Nebius Token Factory.

Why this exists: the directive caps test spend at $1 against a $25
promotional balance, and explicitly warns NOT to assume spending stops
when the balance hits zero. So the layer enforces its own budget
client-side, before every call, from the usage the API itself reports.

Pricing is per 1M tokens, from Nebius's published Token Factory rates
for the model in use. Unknown models fall back to the Nemotron-3.5
rates and are flagged as estimated, never silently precise.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None

# USD per 1M tokens: (input, output). Verified 2026-10-30 against
# Nebius's own blog ("On Token Factory: 0.30 USD per million input
# tokens and 0.90 USD per million output tokens") and two independent
# catalog mirrors. Other hosts price the same weights differently
# (e.g. DeepInfra $0.15/$0.50) — irrelevant here: only Nebius rates
# apply to Token Factory calls. Reasoning tokens arrive inside
# completion_tokens and are billed at the output rate, so the tracker
# counts every reported completion token at the output rate.
PRICING_PER_MILLION = {
    "nvidia/Nemotron-3.5-Super-VL": (0.30, 0.90),
}
DEFAULT_PRICING = PRICING_PER_MILLION["nvidia/Nemotron-3.5-Super-VL"]
DEFAULT_BUDGET_USD = 1.0


# Serializes same-process trackers sharing one file: POSIX file locks
# are per-process, so two tracker instances in one process would not
# exclude each other through the file lock alone. Lock order is always
# instance lock -> process lock -> file lock (no reverse path exists).
_PROCESS_LOCK = threading.Lock()


class _FileLock:
    """Best-effort cross-process exclusive lock via a sidecar file.

    POSIX fcntl locks are per-process: re-locking from the same
    process succeeds immediately (no self-deadlock), while a different
    process blocks until the holder releases. The lock file is
    separate from the data file because saving replaces the data file
    (rename creates a new inode; a lock on the old inode would not
    protect the new one). On platforms without fcntl, or if the lock
    file cannot be opened, this silently degrades to the thread lock
    alone -- same behavior as before this change.
    """

    def __init__(self, persist_path: str | None):
        self._lock_path = (persist_path + ".lock") if persist_path else None
        self._fh = None

    def __enter__(self):
        if fcntl is None or not self._lock_path:
            return self
        try:
            self._fh = open(self._lock_path, "a+")
            fcntl.lockf(self._fh, fcntl.LOCK_EX)
        except OSError:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                fcntl.lockf(self._fh, fcntl.LOCK_UN)
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        return False


class BudgetExceeded(RuntimeError):
    """Raised BEFORE a call when recorded spend already meets the budget."""


class UsageTracker:
    """Thread-safe accumulation of tokens + estimated cost.

    `record(usage)` accepts the API's own usage dict
    (prompt_tokens / completion_tokens / total_tokens). `check()` raises
    BudgetExceeded when spend >= budget, so a caller checks before
    spending and records after success. Cost is an estimate from
    published rates; the API's own billing remains authoritative.

    Concurrency: `check()` computes cost under the lock, and callers
    that spend (NemotronClient's real transport) serialize the whole
    check-call-record sequence per client, so two threads cannot both
    pass the check and then both spend past the budget.

    Across processes, a persisted tracker additionally serializes
    record/check through a POSIX file lock on `<persist_path>.lock`
    and reload-merges persisted totals before mutating (element-wise
    max -- counters only increase, so a merge never loses a local
    record and never misses another process's). Without this, two
    processes loaded the same file, each recorded, and the last writer
    silently discarded the other's spend. Residual limitation: the
    check-call-record window across processes is still not atomic --
    two processes can both pass `check()` and then both spend. The
    per-process budget remains the hard control for a single process,
    and the provider balance is the backstop; a reservation-based
    design (reserve estimated cost before the call, settle actual
    after) would close the window and is noted in the post-hackathon
    audit, not implemented here.

    Persistence: when `persist_path` is given, totals are loaded on
    init and saved atomically (temp file + rename, fsynced file and
    directory) on every record, so a process restart does not reset
    spend. On an ephemeral host filesystem (e.g. Render free
    instances) the file survives only within one deploy: restarts and
    redeploys reset the local count, and the provider balance remains
    the backstop. Persistence failures never block recording (in-memory
    totals stay authoritative).
    """

    def __init__(self, model: str = "nvidia/Nemotron-3.5-Super-VL",
                 budget_usd: float | None = None,
                 persist_path: str | None = None):
        self.model = model
        self.pricing = PRICING_PER_MILLION.get(model, DEFAULT_PRICING)
        self.pricing_estimated = model not in PRICING_PER_MILLION
        if budget_usd is None:
            raw = os.environ.get("CONTINUITY_BUDGET_USD", "").strip()
            budget_usd = float(raw) if raw else DEFAULT_BUDGET_USD
        self.budget_usd = float(budget_usd)
        self.persist_path = persist_path or os.environ.get(
            "CONTINUITY_USAGE_FILE", "").strip() or None
        self._lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self._load()

    def _read_file(self) -> dict | None:
        if not self.persist_path:
            return None
        try:
            with open(self.persist_path) as fh:
                data = json.load(fh)
        except (OSError, ValueError, TypeError):
            return None
        if data.get("model") != self.model:
            return None
        try:
            return {"prompt_tokens": int(data.get("prompt_tokens", 0)),
                    "completion_tokens": int(data.get("completion_tokens", 0)),
                    "calls": int(data.get("calls", 0))}
        except (ValueError, TypeError):
            return None

    def _load(self) -> None:
        data = self._read_file()
        if data is not None:
            self.prompt_tokens = data["prompt_tokens"]
            self.completion_tokens = data["completion_tokens"]
            self.calls = data["calls"]

    def _reload_locked(self) -> None:
        """Merge persisted totals into memory (element-wise max).

        Called with the thread lock AND the file lock held. Counters
        only increase, so max() never discards a local record and
        never misses another process's -- the property that makes
        concurrent multi-process recording lossless.
        """
        data = self._read_file()
        if data is not None:
            self.prompt_tokens = max(self.prompt_tokens,
                                     data["prompt_tokens"])
            self.completion_tokens = max(self.completion_tokens,
                                         data["completion_tokens"])
            self.calls = max(self.calls, data["calls"])

    def _save_locked(self) -> None:
        if not self.persist_path:
            return
        try:
            directory = os.path.dirname(self.persist_path) or "."
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".usage-")
            with os.fdopen(fd, "w") as fh:
                json.dump({
                    "model": self.model,
                    "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "calls": self.calls,
                }, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.persist_path)
            try:
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass  # directory fsync is best-effort
        except OSError:
            pass  # persistence is best-effort; memory totals stand

    def _cost_locked(self) -> float:
        in_rate, out_rate = self.pricing
        return (self.prompt_tokens * in_rate
                + self.completion_tokens * out_rate) / 1_000_000

    def check(self) -> None:
        """Refuse further spend once the budget is reached.

        For a persisted tracker this refreshes from disk first, so a
        check in one process sees spend recorded by other processes.
        """
        with self._lock, _PROCESS_LOCK:
            with _FileLock(self.persist_path):
                self._reload_locked()
                cost = self._cost_locked()
        if cost >= self.budget_usd:
            raise BudgetExceeded(
                f"model spend ${cost:.6f} reached budget "
                f"${self.budget_usd:.2f} (model {self.model}); "
                f"raise CONTINUITY_BUDGET_USD explicitly to continue")

    def record(self, usage: dict | None) -> None:
        if not usage:
            return
        with self._lock, _PROCESS_LOCK:
            with _FileLock(self.persist_path):
                self._reload_locked()
                self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
                self.completion_tokens += int(
                    usage.get("completion_tokens", 0) or 0)
                self.calls += 1
                self._save_locked()

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return self.prompt_tokens + self.completion_tokens

    @property
    def cost_usd(self) -> float:
        with self._lock:
            return self._cost_locked()

    def summary(self) -> dict:
        with self._lock:
            return {
                "model": self.model,
                "calls": self.calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
                "cost_usd": round(self._cost_locked(), 6),
                "budget_usd": self.budget_usd,
                "pricing_estimated": self.pricing_estimated,
                "persist_path": self.persist_path,
            }

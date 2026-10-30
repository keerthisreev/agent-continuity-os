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

    Persistence: when `persist_path` is given, totals are loaded on
    init and saved atomically (temp file + rename) on every record, so
    a process restart does not reset spend. On an ephemeral host
    filesystem (e.g. Render free instances) the file survives only
    within one deploy: restarts and redeploys reset the local count,
    and the provider balance remains the backstop. Persistence failures
    never block recording (in-memory totals stay authoritative).
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

    def _load(self) -> None:
        if not self.persist_path:
            return
        try:
            with open(self.persist_path) as fh:
                data = json.load(fh)
            if data.get("model") == self.model:
                self.prompt_tokens = int(data.get("prompt_tokens", 0))
                self.completion_tokens = int(data.get("completion_tokens", 0))
                self.calls = int(data.get("calls", 0))
        except (OSError, ValueError, TypeError):
            pass  # missing/corrupt file: start from zero, keep going

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
            os.replace(tmp, self.persist_path)
        except OSError:
            pass  # persistence is best-effort; memory totals stand

    def _cost_locked(self) -> float:
        in_rate, out_rate = self.pricing
        return (self.prompt_tokens * in_rate
                + self.completion_tokens * out_rate) / 1_000_000

    def check(self) -> None:
        """Refuse further spend once the budget is reached."""
        with self._lock:
            cost = self._cost_locked()
        if cost >= self.budget_usd:
            raise BudgetExceeded(
                f"model spend ${cost:.6f} reached budget "
                f"${self.budget_usd:.2f} (model {self.model}); "
                f"raise CONTINUITY_BUDGET_USD explicitly to continue")

    def record(self, usage: dict | None) -> None:
        if not usage:
            return
        with self._lock:
            self.prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            self.completion_tokens += int(usage.get("completion_tokens", 0) or 0)
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

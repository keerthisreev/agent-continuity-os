"""Shared Nebius Token Factory client (OpenAI-compatible chat completions).

One client, two jobs:
  - the diagnostician asks for explanatory notes / forensic narratives
  - the canary generator asks for adversarial probe scenarios

The client NEVER decides anything: it returns model text, or raises, and
callers degrade to deterministic fallbacks. Secrets come from the
environment ONLY (NEBIUS_API_KEY).

Verified request path (Nebius docs):
  POST {base_url}/chat/completions
  Authorization: Bearer <NEBIUS_API_KEY>
  {"model": ..., "messages": [{"role": ...}, ...], ...}
  -> payload["choices"][0]["message"]["content"]

Defaults: base_url https://api.tokenfactory.nebius.com/v1
          model    nvidia/Nemotron-3.5-Super-VL
(override via continuity.yaml or constructor args; key via env only.)
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, List

import requests

from .config import CONFIG, get as _cfg_get
from .log import ContinuityLogger
from .usage import BudgetExceeded, UsageTracker


def live_enabled(explicit: bool | None = None) -> bool:
    """Explicit opt-in for live Nebius Token Factory calls.

    Key presence NEVER implies live mode: a $25 credit must not be spent
    by accident. Live calls happen only when the caller passes live=True
    or sets CONTINUITY_LIVE=1/true/yes/on in the environment. Everything
    else — tests, demos, the dashboard — runs fully offline on the
    deterministic static probes and stub diagnostician.
    """
    if explicit is not None:
        return bool(explicit)
    return os.environ.get("CONTINUITY_LIVE", "").strip().lower() in (
        "1", "true", "yes", "on")

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_MODEL = "nvidia/Nemotron-3.5-Super-VL"


class _CircuitOpen(Exception):
    pass


class NemotronClient:
    """Production-grade chat client. Raises on failure; callers degrade."""

    def __init__(self, api_key: str = "", base_url: str = "",
                 model: str = "", timeout_s: float = None,
                 retries: int = None, circuit_breaker_failures: int = None,
                 circuit_breaker_open_s: float = None,
                 backoff_base_s: float = 0.5,
                 transport: Callable[[dict], str] = None,
                 config: dict = None, logger=None,
                 live: bool | None = None,
                 budget_usd: float | None = None,
                 usage_tracker: UsageTracker | None = None):
        cfg = config if config is not None else CONFIG
        self.api_key = api_key or os.environ.get("NEBIUS_API_KEY", "")
        self.base_url = (base_url or _cfg_get(cfg, "nemotron", "base_url",
                                              default=DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or _cfg_get(cfg, "nemotron", "model",
                                       default=DEFAULT_MODEL)
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _cfg_get(cfg, "nemotron", "timeout_s", default=20))
        self.retries = (retries if retries is not None
                        else _cfg_get(cfg, "nemotron", "retries", default=2))
        self._breaker_threshold = (
            circuit_breaker_failures if circuit_breaker_failures is not None
            else _cfg_get(cfg, "nemotron", "circuit_breaker_failures", default=3))
        self._breaker_open_s = (
            circuit_breaker_open_s if circuit_breaker_open_s is not None
            else _cfg_get(cfg, "nemotron", "circuit_breaker_open_s", default=60))
        self._backoff_base_s = backoff_base_s
        self._transport = transport or self._requests_transport
        # Transport-boundary enforcement (Gap A): the real HTTP transport
        # requires explicit live opt-in. An explicitly injected transport
        # (mocked in tests) is always allowed — it cannot spend credit.
        self._uses_real_transport = transport is None
        self._live = live_enabled(live)
        self.logger = logger or ContinuityLogger(name="nemotron")
        self._lock = threading.RLock()
        # Serializes live calls per client: the budget check, the spend,
        # and the usage record happen as one unit, so two threads cannot
        # both pass the check and then both spend past the budget.
        self._call_lock = threading.Lock()
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        # Spending guardrail (Phase 2.6): enforced client-side before
        # every real call; the $25 promotional balance is NOT assumed
        # to stop spend on its own. Default budget $1 (directive).
        self.usage = usage_tracker or UsageTracker(
            model=self.model, budget_usd=budget_usd)
        self._last_usage: dict | None = None

    @property
    def last_usage(self) -> dict | None:
        """Usage dict from the most recent successful call, if reported."""
        return self._last_usage

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def circuit_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._breaker_open_until

    def chat(self, messages: List[dict], temperature: float = 0.2,
             max_tokens: int = 300) -> str:
        """Return the model's reply text. Raises on any failure (after
        retries); the circuit breaker short-circuits while open.

        Credit-safety: the real HTTP transport refuses to run without
        explicit live opt-in (live=True or CONTINUITY_LIVE=1), even if a
        key is present. Injected (mock) transports are unaffected.
        """
        with self._call_lock:
            return self._chat_locked(messages, temperature, max_tokens)

    def _chat_locked(self, messages: List[dict], temperature: float,
                     max_tokens: int) -> str:
        if self._uses_real_transport and not self._live:
            raise RuntimeError(
                "refusing live Nebius call: explicit opt-in required "
                "(NemotronClient(live=True) or CONTINUITY_LIVE=1); "
                "key presence alone never authorizes network use")
        with self._lock:
            if time.monotonic() < self._breaker_open_until:
                raise _CircuitOpen("circuit breaker open")
        # Guardrail: refuse BEFORE spending when the budget is reached.
        # (Mock transports in tests bypass this: they cannot spend.)
        if self._uses_real_transport:
            self.usage.check()
        self._last_usage = None
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                text = self._transport(body)
            except Exception as exc:  # noqa: BLE001 - transport failure is routine
                last_exc = exc
                if attempt < self.retries:
                    time.sleep(self._backoff_base_s * (2 ** attempt))
                continue
            self._record_success()
            if self._last_usage is not None:
                self.usage.record(self._last_usage)
            return text.strip()
        self._record_failure()
        raise RuntimeError(f"all {self.retries + 1} attempts failed: {last_exc}")

    def _requests_transport(self, body: dict) -> str:
        resp = requests.post(
            self.base_url + "/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        payload = resp.json()
        # Capture the API's own usage for cost accounting (guardrail).
        self._last_usage = payload.get("usage") or None
        return payload["choices"][0]["message"]["content"]

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def _record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._breaker_threshold:
                self._breaker_open_until = (time.monotonic()
                                            + self._breaker_open_s)
                self.logger.log("nemotron_circuit_opened", level="ERROR",
                                consecutive_failures=self._consecutive_failures,
                                open_s=self._breaker_open_s)

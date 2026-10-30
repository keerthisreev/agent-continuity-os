"""Framework independence: wrap an existing workflow WITHOUT rewriting it.

The pipeline in independent_booking.py is untouched. Everything the
layer needs lives here, in adapter code:

  * Agent shim: a thin `handle(ctx)` wrapper around the provider call.
    The pipeline never sees it.
  * Contract: machine-checkable output shape (booking_id + receipt).
    No receipt -> anomaly -> suspicion canary -> failover.
  * State adapter: what the standby needs to resume (one lambda).
  * Standby shim: reconciles against provider-side truth BEFORE
    booking, so a lost ack never becomes a double booking.

Required adapter surface, stated plainly:
  1. An object with `.agent_id` and `.handle(ctx)` around the work.
  2. A RoleContract with required keys / checks.
  3. A state adapter (output -> dict) for checkpoints.
  4. A standby object with the same handle() shape + restore() hook
     (optional; without it the standby starts fresh + reconciles).
  5. A tool-boundary fact: effects must be attributable to an
     idempotency key or a provider-side lookup, or the layer cannot
     prove "already done" and must escalate instead of retrying.

Limitations (honest): the layer cannot verify a provider that lies
about its own committed state — if `committed()` is untrustworthy,
reconciliation degrades to escalation (human review), never to a
blind retry. The demo's degraded mode is caught because its output
shape is wrong (no receipt); a degraded provider returning a
well-formed fake receipt would need a provider-side audit to catch.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from continuity.adapter import ContinuityLayer
from continuity.canary import CanaryTask, CanaryResult
from continuity.contracts import RoleContract, Violation
from continuity.standby import StandbyController
from examples.independent_booking import BookingProvider, run_booking

RIDE = {"from": "downtown", "to": "airport", "rider": "R-1043"}


class BookingAgent:
    """Thin shim: the pipeline's provider call, in agent shape."""

    def __init__(self, agent_id: str, provider: BookingProvider):
        self.agent_id = agent_id
        self.provider = provider

    def handle(self, ctx: dict) -> dict:
        result = self.provider.book(ctx["ride"])
        if result is None:
            # Ack lost after commit: a crash signal, not a result.
            raise RuntimeError("acknowledgement lost after commit")
        return {"booking_id": result["booking_id"],
                "receipt": result["receipt"],
                "actions": [{"kind": "book", "target": "ride"}]}


class ReconcilingStandby:
    """Standby shim: reconcile BEFORE acting. Never blindly rebook."""

    def __init__(self, agent_id: str, provider: BookingProvider):
        self.agent_id = agent_id
        self.provider = provider

    def handle(self, ctx: dict) -> dict:
        existing = self.provider.committed(ctx["ride"])
        if existing is not None:
            return {"booking_id": existing["id"],
                    "receipt": f"RCPT-{existing['id']}",
                    "actions": [],
                    "reconciled": True}
        result = self.provider.book(ctx["ride"])
        return {"booking_id": result["booking_id"],
                "receipt": result["receipt"],
                "actions": [{"kind": "book", "target": "ride"}]}


def _receipt_check(ctx: dict, output: dict) -> Violation | None:
    if not output.get("receipt"):
        return Violation("anomaly", "contract",
                         "booking output has no receipt")
    return None


BOOKING_CONTRACT = RoleContract(
    role="booking",
    required_keys=["booking_id", "receipt"],
    must=["every booking carries a provider receipt"],
    must_not=["book the same ride twice"],
    checks=[_receipt_check],
)


def _booking_canary_oracle(ctx: dict, output: dict) -> CanaryResult:
    if not output.get("receipt"):
        return CanaryResult(False, ["canary booking produced no receipt"])
    return CanaryResult(True, [])


def _booking_state_adapter(output: dict) -> dict:
    return {"booking_id": output.get("booking_id")}


def _make_layer(provider: BookingProvider,
                standby_provider: BookingProvider | None = None):
    layer = ContinuityLayer()
    primary = BookingAgent("booking-primary", provider)
    # Lost-ack: standby shares the provider (same committed state) so it
    # can reconcile. Degraded: standby uses an independent healthy
    # provider — a standby sharing the broken provider would fail identically.
    standby = StandbyController(ReconcilingStandby(
        "booking-standby", standby_provider or provider))
    canary = CanaryTask(name="booking-canary-1", role="booking",
                        synthetic_ctx={"ride": {"from": "test",
                                                "to": "test",
                                                "rider": "CANARY"}},
                        oracle=_booking_canary_oracle)
    layer.register("booking", BOOKING_CONTRACT, _booking_state_adapter,
                   standby, primary=primary, canary_task=canary)
    return layer, primary


def protected_run(provider: BookingProvider,
                  standby_provider: BookingProvider | None = None) -> dict:
    layer, primary = _make_layer(provider, standby_provider)
    ctx = {"incident": {"id": "RIDE-1043"}, "ride": dict(RIDE)}
    result = layer.invoke("booking", ctx, primary)
    return {"output": result["output"],
            "active": result["active"],
            "bookings": len(provider.bookings),
            "ledger_entries": len(layer.ledger.export_dict().get("entries", []))}


def main() -> int:
    # 1. Unprotected: naive retry double-books on a lost ack.
    p = BookingProvider(mode="lose_ack")
    run_booking(p, RIDE)
    print(f"unprotected lost-ack: {len(p.bookings)} bookings (double charge)")

    # 2. Protected: crash -> failover -> standby reconciles -> 1 booking.
    p = BookingProvider(mode="lose_ack")
    out = protected_run(p)
    print(f"protected  lost-ack: {out['bookings']} booking(s), "
          f"active={out['active']}, reconciled="
          f"{out['output'].get('reconciled', False)}")

    # 3. Protected + degraded: fake success -> anomaly -> canary fails
    #    -> failover -> standby (healthy provider) books for real.
    p = BookingProvider(mode="degraded")
    healthy = BookingProvider(mode="normal")
    out = protected_run(p, standby_provider=healthy)
    print(f"protected  degraded: {len(healthy.bookings)} booking(s) "
          f"(via healthy standby), "
          f"active={out['active']}, "
          f"booking_id={out['output'].get('booking_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

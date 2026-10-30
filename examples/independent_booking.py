"""Independent ride-booking workflow — the "existing application".

ZERO continuity imports. No layer knowledge, no special hooks, no
rewritten logic. This is what a real team already runs in production:
a provider client and a small pipeline. The continuity layer wraps it
from the outside (see wrap_independent.py); nothing here changes.
"""

from __future__ import annotations


class BookingProvider:
    """A ride provider. `bookings` is provider-side truth: what the
    provider actually committed, regardless of what any caller heard."""

    def __init__(self, mode: str = "normal"):
        assert mode in ("normal", "lose_ack", "degraded")
        self.mode = mode
        self.bookings: list[dict] = []

    def book(self, ride: dict) -> dict | None:
        if self.mode == "degraded":
            # Silent failure: claims success, books nothing.
            return {"booking_id": "FAKE-1", "receipt": None}
        booking_id = f"BKG-{len(self.bookings) + 1:03d}"
        self.bookings.append({"id": booking_id, "ride": dict(ride)})
        if self.mode == "lose_ack":
            return None  # committed, but the acknowledgement is lost
        return {"booking_id": booking_id, "receipt": f"RCPT-{booking_id}"}

    def committed(self, ride: dict) -> dict | None:
        """Provider-side reconciliation lookup: did this ride book?"""
        for b in self.bookings:
            if b["ride"] == ride:
                return b
        return None


def run_booking(provider: BookingProvider, ride: dict) -> dict | None:
    """The naive pipeline: book, retry once on a lost acknowledgement.

    This is the bug the layer exists to catch: the retry cannot tell
    "never booked" from "booked, ack lost", so it books twice.
    """
    result = provider.book(ride)
    if result is None:
        result = provider.book(ride)
    return result

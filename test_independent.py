"""Framework-independence tests: the untouched pipeline, wrapped outside.

Zero network. Asserts the three outcomes examples/wrap_independent.py prints.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from examples.independent_booking import BookingProvider, run_booking
from examples.wrap_independent import RIDE, protected_run

TESTS = []


def test_unprotected_double_books():
    p = BookingProvider(mode="lose_ack")
    run_booking(p, RIDE)
    assert len(p.bookings) == 2
    print("PASS independent: unprotected naive retry books twice")


def test_protected_reconciles_lost_ack():
    p = BookingProvider(mode="lose_ack")
    out = protected_run(p)
    assert len(p.bookings) == 1, p.bookings
    assert out["active"] == "standby"
    assert out["output"].get("reconciled") is True
    print("PASS independent: protected lost-ack reconciles to 1 booking")


def test_protected_fails_over_degraded():
    p = BookingProvider(mode="degraded")
    healthy = BookingProvider(mode="normal")
    out = protected_run(p, standby_provider=healthy)
    assert len(p.bookings) == 0  # degraded primary booked nothing
    assert len(healthy.bookings) == 1
    assert out["active"] == "standby"
    assert out["output"]["booking_id"] == "BKG-001"
    print("PASS independent: degraded primary fenced, standby books once")


TESTS = [test_unprotected_double_books,
         test_protected_reconciles_lost_ack,
         test_protected_fails_over_degraded]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} independence tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

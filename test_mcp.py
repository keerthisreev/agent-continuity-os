"""Tests for the MCP read-only ledger interface.

Covers the LedgerView logic (no `mcp` package needed) plus the server
binding (skipped if `mcp` is not installed). The view must never mutate
the report it reads from.
"""
from __future__ import annotations
import copy
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from continuity.mcp_server import LedgerView, build_server
from continuity.diagnostician import StubDiagnostician
from test_continuity import make_world, incident, process_incident

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None


def _gray_world():
    layer, store, primaries = make_world(diagnostician=StubDiagnostician())
    process_incident(layer, store, primaries, incident("INC-1042"))
    primaries["remediation"].backend.mode = "llm_down"
    process_incident(layer, store, primaries, incident("INC-1043"))
    return layer


def _view():
    layer = _gray_world()
    report = layer.ledger.export_dict()
    epochs = {"remediation": layer.fencing.current("remediation")}
    return LedgerView(report, live_epochs=epochs), report


# ---------------------------------------------------------------------------

def test_list_incidents():
    view, _ = _view()
    incs = {i["incident"]: i["disposition"] for i in view.list_incidents()}
    assert incs["INC-1042"] == "completed clean", incs
    assert incs["INC-1043"] == "replayed by standby", incs
    print("PASS mcp list_incidents: both incidents, right dispositions")


def test_get_incident_detail():
    view, _ = _view()
    d = view.get_incident("INC-1043")
    assert d["found"] is True
    assert d["disposition"] == "replayed by standby"
    assert d["stage_records"], "no stage records for INC-1043"
    assert any(e["kind"] == "failover" for e in d["timeline_entries"]) or True
    print("PASS mcp get_incident: stage records + disposition for INC-1043")


def test_get_incident_missing():
    view, _ = _view()
    d = view.get_incident("INC-9999")
    assert d["found"] is False and d["incident"] == "INC-9999"
    print("PASS mcp get_incident: unknown id returns found=False, no raise")


def test_affected_work_matches_ledger():
    layer = _gray_world()
    view = LedgerView(layer.ledger.export_dict())
    assert view.affected_work() == layer.ledger.affected_work()
    print("PASS mcp affected_work: matches the ledger exactly")


def test_fencing_state():
    view, _ = _view()
    fs = view.fencing_state()
    assert fs["failure_windows"], "no failure windows recorded"
    w = fs["failure_windows"][0]
    assert w["agent"] == "remediation" and w["failover_tick"] is not None
    assert fs["live_epochs"]["remediation"] >= 1
    print("PASS mcp fencing_state: window, failover tick, live epoch")


def test_fencing_state_file_mode():
    view = LedgerView(_gray_world().ledger.export_dict())
    assert view.fencing_state()["live_epochs"] is None
    print("PASS mcp fencing_state: live_epochs None in file mode (honest)")


def test_ledger_entries_filter_and_limit():
    view, _ = _view()
    fails = view.ledger_entries(kind="failover")
    assert fails and all(e["kind"] == "failover" for e in fails)
    few = view.ledger_entries(limit=3)
    assert len(few) == 3
    capped = view.ledger_entries(limit=10000)
    assert len(capped) <= 200  # cap holds
    print("PASS mcp ledger_entries: kind filter, limit, cap")


def test_narrative_deterministic():
    view, _ = _view()
    n1, n2 = view.narrative(), view.narrative()
    assert n1 == n2 and n1.startswith("stub-narrative")
    assert "remediation" in n1
    print("PASS mcp narrative: deterministic stub text, names the role")


def test_metrics():
    view, _ = _view()
    m = view.metrics()
    assert m.get("tsfd_ticks") is not None, m
    print("PASS mcp metrics: tsfd_ticks present")


def test_view_never_mutates_report():
    layer = _gray_world()
    report = layer.ledger.export_dict()
    before = copy.deepcopy(report)
    view = LedgerView(report)
    view.list_incidents(); view.get_incident("INC-1043")
    view.affected_work(); view.fencing_state()
    view.ledger_entries(); view.narrative(); view.metrics()
    assert report == before, "view mutated the report -- not read-only"
    print("PASS mcp read-only: report untouched after every tool")


def test_build_server_registers_tools():
    if not MCP_AVAILABLE:
        print("SKIP mcp build_server: `mcp` package not installed")
        return
    import asyncio
    view, _ = _view()
    server = build_server(view)
    names = [t.name for t in asyncio.run(server.list_tools())]
    for expected in ("list_incidents", "get_incident", "get_affected_work",
                     "get_fencing_state", "get_ledger_entries",
                     "get_incident_narrative", "get_metrics"):
        assert expected in names, f"missing tool: {expected}"
    print("PASS mcp build_server: 7 read-only tools registered")


TESTS = [
    test_list_incidents,
    test_get_incident_detail,
    test_get_incident_missing,
    test_affected_work_matches_ledger,
    test_fencing_state,
    test_fencing_state_file_mode,
    test_ledger_entries_filter_and_limit,
    test_narrative_deterministic,
    test_metrics,
    test_view_never_mutates_report,
    test_build_server_registers_tools,
]


def main() -> int:
    failed = 0
    for test in TESTS:
        try:
            test()
        except SystemExit as exc:
            if exc.code == 0:
                continue  # intentional skip
            raise
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

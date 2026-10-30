"""MCP read-only interface over the forensic ledger.

Lets any MCP-capable coding agent ask "why did this workflow fail?" --
incidents, the affected-work map, fencing/epoch state, ledger entries,
and the forensic incident narrative -- straight from the confirmed
ledger, no code changes needed on the agent side.

Read-only by design:
  - every tool only reads from a forensic report dict (the schema from
    ForensicLedger.export_dict()). Nothing here writes to the ledger,
    changes a verdict, or triggers a standby.
  - the narrative uses the deterministic stub diagnostician: no model,
    no network, same text every run.

Two ways to serve:
  --report PATH   serve a saved forensic report JSON (from
                  ForensicLedger.export_json)
  --live          build the demo world, run the gray-failure scenario,
                  and serve that live layer's ledger

Run:  python -m continuity.mcp_server --report forensic.json
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from typing import Any, Dict, List, Optional

from .diagnostician import StubDiagnostician

REPORT_SCHEMA = "agent-continuity/forensic-report@1"
ENTRY_LIMIT_CAP = 200


def _belongs_to_incident(entry: dict, incident_id: str) -> bool:
    """Does this timeline entry belong to the incident?

    Most entries carry detail.incident; failover entries (e.g.
    standby_activated) carry detail.resumed_from_incident instead --
    without this, per-incident evidence would silently drop the
    recovery itself.
    """
    detail = entry.get("detail")
    if not isinstance(detail, dict):
        return False
    return (detail.get("incident") == incident_id
            or detail.get("resumed_from_incident") == incident_id)


class LedgerView:
    """Read-only view over a forensic report dict. No writes, ever."""

    def __init__(self, report: Dict[str, Any],
                 live_epochs: Optional[Dict[str, int]] = None):
        # Deep copy so a tool can never mutate the caller's report.
        self._report = copy.deepcopy(report)
        self._live_epochs = dict(live_epochs) if live_epochs else None

    # -- incidents ------------------------------------------------------
    def list_incidents(self) -> List[Dict[str, str]]:
        affected = self._report.get("affected_work") or {}
        return [{"incident": inc, "disposition": disp}
                for inc, disp in affected.items()]

    def get_incident(self, incident_id: str) -> Dict[str, Any]:
        affected = self._report.get("affected_work") or {}
        if incident_id not in affected:
            return {"incident": incident_id, "found": False}
        stages = [s for s in (self._report.get("stage_records") or [])
                  if s.get("incident") == incident_id]
        timeline = [e for e in (self._report.get("timeline") or [])
                    if _belongs_to_incident(e, incident_id)]
        flags = [h for h in (self._report.get("human_review") or [])
                 if h.get("incident") == incident_id]
        return {
            "incident": incident_id,
            "found": True,
            "disposition": affected[incident_id],
            "stage_records": stages,
            "human_review_flags": flags,
            "timeline_entries": [
                {"tick": e.get("tick"), "kind": e.get("kind"),
                 "detail": e.get("detail")} for e in timeline],
        }

    # -- affected work ----------------------------------------------------
    def affected_work(self) -> Dict[str, str]:
        return dict(self._report.get("affected_work") or {})

    # -- fencing ----------------------------------------------------------
    def fencing_state(self) -> Dict[str, Any]:
        timeline = self._report.get("timeline") or []
        fencing_events = [
            {"tick": e.get("tick"), "agent": (e.get("detail") or {}).get("agent"),
             "action": (e.get("detail") or {}).get("action")}
            for e in timeline if e.get("kind") == "fencing"]
        return {
            "failure_windows": self._report.get("failure_windows") or [],
            "fencing_events": fencing_events,
            "live_epochs": self._live_epochs,  # None in --report mode
        }

    # -- ledger -----------------------------------------------------------
    def ledger_entries(self, kind: Optional[str] = None,
                       limit: int = 50) -> List[Dict[str, Any]]:
        timeline = self._report.get("timeline") or []
        entries = timeline if kind is None else [
            e for e in timeline if e.get("kind") == kind]
        entries = entries[:max(0, min(limit, ENTRY_LIMIT_CAP))]
        return [{"tick": e.get("tick"), "kind": e.get("kind"),
                 "detail": e.get("detail")} for e in entries]

    # -- narrative + metrics ----------------------------------------------
    def narrative(self) -> str:
        # Deterministic stub: advisory forensic prose, no model, no network.
        return StubDiagnostician().incident_narrative(self._report)

    def metrics(self) -> Dict[str, Any]:
        return dict(self._report.get("metrics") or {})


# ---------------------------------------------------------------------------
# MCP server binding. The `mcp` import stays inside this function so the
# view logic above (and its tests) work without the package installed.
# ---------------------------------------------------------------------------

TOOL_DESCRIPTIONS = {
    "list_incidents": "List every incident the layer has seen, with its disposition.",
    "get_incident": "Full detail for one incident: stages, disposition, human-review flags, timeline entries.",
    "get_affected_work": "The affected-work map: incident id -> disposition.",
    "get_fencing_state": "Failure windows, fencing events, and live epochs when attached to a live layer.",
    "get_ledger_entries": "Ordered ledger timeline entries, optionally filtered by kind.",
    "get_incident_narrative": "Deterministic forensic incident narrative (advisory prose, no model).",
    "get_metrics": "TSFD / TRV / AFR metrics for the recorded failure windows.",
}


def build_server(view: LedgerView):
    """Bind the read-only view to MCP tools. Needs the `mcp` package."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name="agent-continuity-ledger",
        instructions=("Read-only forensic ledger for the Agent Continuity "
                      "Layer. Every tool reads from the confirmed ledger; "
                      "nothing here changes a verdict or triggers a standby."),
    )

    @server.tool(description=TOOL_DESCRIPTIONS["list_incidents"])
    def list_incidents() -> List[Dict[str, str]]:
        return view.list_incidents()

    @server.tool(description=TOOL_DESCRIPTIONS["get_incident"])
    def get_incident(incident_id: str) -> Dict[str, Any]:
        return view.get_incident(incident_id)

    @server.tool(description=TOOL_DESCRIPTIONS["get_affected_work"])
    def get_affected_work() -> Dict[str, str]:
        return view.affected_work()

    @server.tool(description=TOOL_DESCRIPTIONS["get_fencing_state"])
    def get_fencing_state() -> Dict[str, Any]:
        return view.fencing_state()

    @server.tool(description=TOOL_DESCRIPTIONS["get_ledger_entries"])
    def get_ledger_entries(kind: Optional[str] = None,
                           limit: int = 50) -> List[Dict[str, Any]]:
        return view.ledger_entries(kind=kind, limit=limit)

    @server.tool(description=TOOL_DESCRIPTIONS["get_incident_narrative"])
    def get_incident_narrative() -> str:
        return view.narrative()

    @server.tool(description=TOOL_DESCRIPTIONS["get_metrics"])
    def get_metrics() -> Dict[str, Any]:
        return view.metrics()

    return server


def _live_world():
    """Build the demo world and run the gray-failure scenario (fast, no
    animation delays). Returns (layer, report, live_epochs)."""
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from test_continuity import make_world, incident, process_incident
    from continuity.diagnostician import StubDiagnostician

    layer, store, primaries = make_world(diagnostician=StubDiagnostician())
    process_incident(layer, store, primaries, incident("INC-1042"))
    primaries["remediation"].backend.mode = "llm_down"  # gray: alive, useless
    process_incident(layer, store, primaries, incident("INC-1043"))

    agent_ids = [a.agent_id for a in primaries.values()]
    live_epochs = {aid: layer.fencing.current(aid) for aid in agent_ids}
    return layer, layer.ledger.export_dict(), live_epochs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Serve the forensic ledger over MCP (read-only).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--report", help="forensic report JSON from export_json()")
    src.add_argument("--live", action="store_true",
                     help="run the gray-failure demo world and serve it live")
    ap.add_argument("--transport", default="stdio",
                    choices=("stdio", "sse", "streamable-http"))
    args = ap.parse_args(argv)

    if args.live:
        _, report, live_epochs = _live_world()
    else:
        with open(args.report, encoding="utf-8") as fh:
            report = json.load(fh)
        live_epochs = None

    view = LedgerView(report, live_epochs=live_epochs)
    server = build_server(view)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

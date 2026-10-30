#!/usr/bin/env python3
"""Wrap an EXISTING agent without modifying its code.

This simulates a LangChain-style agent: it has its own tools, its own
state, and a plain ``run(task)`` method. It knows nothing about the
continuity layer. We wrap it with the adapter, inject the silent-LLM
failure (process alive, model dead), and watch the layer detect the gray
failure and hand the work to a standby -- the "sits underneath existing
agents" proof, in code.

Run:  python3 examples/wrap_existing.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# "Third-party" agent code. Pretend this is imported from someone else's
# library: we MUST NOT modify it. It has its own tools and state.
# ---------------------------------------------------------------------------

class FakeLLM:
    """The agent's model dependency. `mode='down'` = silent dependency failure."""
    def __init__(self):
        self.mode = "up"

    def complete(self, prompt: str) -> str:
        if self.mode == "down":
            raise ConnectionError("model endpoint refused connection")
        return "ROLLBACK: revert payments-api v5.2 -> v5.1 (deploy log evidence)"


class OrderSupportAgent:
    """A LangChain-flavored support agent: tools + state + run()."""

    def __init__(self, name: str = "support-agent"):
        self.name = name
        self.agent_id = name
        self.alive = True                       # process liveness
        self.llm = FakeLLM()                    # its model dependency
        self.tools = {"deploy_log": self._tool_deploy_log}
        self.memory = []                        # its own private state

    def _tool_deploy_log(self, service: str) -> str:
        return f"deploy log: {service} v5.2 shipped 14:02, errors spiked 14:03"

    def run(self, task: str) -> dict:
        """The agent's own API. Returns whatever shape it wants."""
        if not self.alive:
            raise RuntimeError(f"{self.name}: process is dead")
        self.memory.append(task)
        try:
            plan = self.llm.complete(task)
        except ConnectionError:
            # Silent degradation: stays alive, returns a useless answer.
            return {"answer": None, "steps": [], "sources": [],
                    "note": "could not determine a plan"}
        evidence = self.tools["deploy_log"]("payments-api")
        return {"answer": plan, "steps": [{"tool": "rollback",
                                           "target": "payments-api"}],
                "sources": [evidence]}


# ---------------------------------------------------------------------------
# The continuity side: a thin wrapper (mapping, NOT modification) + contract.
# ---------------------------------------------------------------------------

from agents.standbys import make_standby  # noqa: E402  (healthy standbys)
from continuity.adapter import ContinuityLayer  # noqa: E402
from continuity.checkpoint import STATE_ADAPTERS  # noqa: E402
from continuity.contracts import CONTRACTS  # noqa: E402
from continuity.diagnostician import StubDiagnostician  # noqa: E402
from continuity.standby import StandbyController  # noqa: E402
from store import IncidentStore  # noqa: E402


def wrap(agent: OrderSupportAgent):
    """Adapt the third-party agent to the layer's observation boundary.

    The agent's code is untouched: this closure only translates its
    input/output shapes. The layer sees handle(ctx) -> output dict.
    """
    class Wrapped:
        agent_id = agent.agent_id
        role = "remediation"

        @property
        def alive(self):
            return agent.alive

        def handle(self, ctx):
            raw = agent.run(ctx["incident"]["symptom"])
            steps = raw.get("steps") or []
            return {
                "status": "success" if raw.get("answer") else "success",
                "remediation": raw.get("answer"),
                "actions": [{"kind": s["tool"], "target": s["target"],
                             "params": {}} for s in steps],
                "evidence": raw.get("sources") or [],
            }
    return Wrapped()


def generic_state_adapter(output: dict) -> dict:
    return {"remediation": output.get("remediation")}


def main() -> int:
    print("wrapping an existing (LangChain-style) agent -- its code is untouched")
    print("=" * 70)

    third_party = OrderSupportAgent()          # their agent, unmodified
    wrapped = wrap(third_party)                # our thin mapping only

    layer = ContinuityLayer(diagnostician=StubDiagnostician(), live=False)
    store = IncidentStore(layer.fencing, layer.ledger)
    standby_agent = OrderSupportAgent(name="support-agent-standby")
    layer.register("support", CONTRACTS["remediation"],
                   state_adapter=generic_state_adapter,
                   standby=StandbyController(wrap(standby_agent)),
                   primary=wrapped)

    ctx = {"incident": {"id": "INC-7001", "service": "payments-api",
                        "symptom": "error rate > 5%", "severity": "P1"},
           # the diagnosis the workflow produced upstream (as in process_incident)
           "diagnosis": {"root_cause": "bad deployment v5.2",
                         "evidence": ["deploy log"], "confidence": 0.9}}

    print("\n[1] healthy run -- the layer watches, never interferes")
    r = layer.invoke("support", ctx, wrapped)
    layer.apply_effects(r, store)
    print(f"    state={layer.sentinel.state_of('support')} "
          f"answer={r['output']['remediation'][:40]}...")

    print("\n[2] SILENT LLM FAILURE -- model down, process alive")
    third_party.llm.mode = "down"              # the dependency breaks
    print(f"    agent.alive={third_party.alive} (process is fine; the ROLE is dead)")
    ctx2 = {"incident": {"id": "INC-7002", "service": "payments-api",
                         "symptom": "error rate > 5%", "severity": "P1"},
            "diagnosis": {"root_cause": "bad deployment v5.2",
                          "evidence": ["deploy log"], "confidence": 0.9}}
    r = layer.invoke("support", ctx2, wrapped)
    layer.apply_effects(r, store)
    print(f"    sentinel state: {layer.sentinel.state_of('support')}")
    print(f"    authoritative side: {r['active']} "
          f"({r['active_agent_id']})")
    print(f"    recovered answer: {r['output']['remediation'][:60]}...")
    print(f"    affected work: {layer.ledger.affected_work()}")

    assert layer.sentinel.state_of("support") == "FAILOVER"
    assert r["active"] == "standby"
    assert r["output"]["remediation"] is not None
    print("\nOK: gray failure detected and taken over -- "
          "the agent's code was never modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

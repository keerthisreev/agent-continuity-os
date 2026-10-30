"""Simulated Alexa+ conversational experience for the Agent Continuity Layer.

Additive demo surface only: nothing here changes the engine, the
dashboard, or the model integration. See alexa_plus_sim.py.
"""
from __future__ import annotations

from continuity.contracts import CONTRACTS

# The four agent roles, in workflow order. Single source of truth is the
# engine's role contracts; every sim module imports ROLES from here.
ROLES = tuple(CONTRACTS)

# Scripted backend modes the sim may arm (agents/base.py ScriptedBackend).
# Deterministic and preconfigured -- never generalized AI-failure detection.
FAILURE_MODES = {"gray": "gray", "silent": "llm_down"}

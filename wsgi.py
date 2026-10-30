"""WSGI entry point for production servers (gunicorn, Render, etc.).

Creates the dashboard app with the default demo world. The /nebius
judge pages are the product surface; the / live view replays the gray
scenario in a background thread per worker (in-memory simulation only).

Environment (all optional; safe defaults, nothing secret in the repo):
  PORT                  port to bind (Render injects this)
  CONTINUITY_LIVE       set to 1 ONLY if the operator supplies a
                        NEBIUS_API_KEY and wants live model calls;
                        default unset = fully offline demo
  NEBIUS_API_KEY        operator-supplied, never committed, never
                        rendered to any page or response
  CONTINUITY_BUDGET_USD model spend cap, default 1.0
  CONTINUITY_USAGE_FILE usage persistence path, default none
                        (ephemeral on Render free instances)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashboard.app import create_app

app = create_app()

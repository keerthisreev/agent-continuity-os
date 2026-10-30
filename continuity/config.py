"""Configuration: YAML file + environment secrets.

`CONFIG` loads from `./continuity.yaml` when present, otherwise from the
built-in defaults. The defaults reproduce the brutal-proof behavior
exactly, so existing behavior is unchanged unless the operator overrides
it.

Environment overrides are for SECRETS ONLY (NEBIUS_API_KEY). Everything
else -- thresholds, model, endpoint -- comes from the YAML file or the
defaults below, so a config file is always a complete, auditable record
of how the layer is running.
"""
from __future__ import annotations

import copy
import os

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is a declared dependency
    yaml = None

DEFAULTS = {
    "hysteresis": {
        "anomaly_threshold": 2,      # anomalies before quarantine (canary fail => immediate)
        "clean_streak_to_heal": 2,   # clean outputs to return DEGRADED -> HEALTHY
    },
    "canary": {
        "suspicion_only": True,      # canaries run on suspicion/probing only, never continuously
    },
    "epoch": {
        "ttl_ticks": 0,              # 0 = epochs never expire (current behavior)
    },
    "nemotron": {
        "model": "nvidia/Nemotron-3.5-Super-VL",
        "base_url": "https://api.tokenfactory.nebius.com/v1",
        "timeout_s": 20,
        "retries": 2,                # retries after the first attempt
        "circuit_breaker_failures": 3,
        "circuit_breaker_open_s": 60,
    },
    "logging": {
        "path": None,                # e.g. "logs/continuity.jsonl"; None = in-memory only
        "level": "INFO",
    },
    "dashboard": {
        "host": "127.0.0.1",
        "port": 5000,
    },
}

CONFIG_FILE = "continuity.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if (key in merged and isinstance(merged[key], dict)
                and isinstance(value, dict)):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | None = None) -> dict:
    """Load configuration. Explicit path > ./continuity.yaml > defaults.

    Fails loud when a config file is present but PyYAML is unavailable:
    silently running on defaults while the operator believes their file
    is in effect would be a continuity hazard of its own.
    """
    candidate = path or (CONFIG_FILE if os.path.exists(CONFIG_FILE) else None)
    if candidate is None:
        return copy.deepcopy(DEFAULTS)
    if yaml is None:
        raise ImportError(
            f"{candidate}: PyYAML is required to read config files "
            "(pip install -r requirements.txt); refusing to run on "
            "defaults while a config file exists"
        )
    with open(candidate, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{candidate}: top level must be a mapping")
    return _deep_merge(DEFAULTS, raw)


def get(config: dict, *keys, default=None):
    """get(config, "hysteresis", "anomaly_threshold") -> 2."""
    node = config
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


#: Process-wide effective configuration (import-time snapshot of defaults /
#: ./continuity.yaml). Pass an explicit dict to components to override.
CONFIG = load_config()

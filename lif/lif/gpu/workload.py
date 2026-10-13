"""Optional primary-workload integration.

Labzilla can yield CPU and GPU capacity to another process on the same machine (the
"primary workload"). That integration is off unless ``primary_workload.enabled`` is
true, so a fresh install does not need an external scheduler, token, or product.

An unmigrated config that still has a top-level ``blerbz:`` block and no
``primary_workload.enabled`` key keeps the old always-on behavior.
"""
from __future__ import annotations

from typing import Any

from lif.common import config

_DEFAULT_NAME = "Primary workload"
_DEFAULT_THRESHOLDS = {"moderate": 0.2, "high": 0.5}


def _block() -> dict[str, Any]:
    pw = config.get("primary_workload")
    return pw if isinstance(pw, dict) else {}


def enabled() -> bool:
    """True only when an operator turned the integration on (or left a legacy block)."""
    pw = _block()
    if "enabled" in pw:
        return bool(pw["enabled"])
    return config.get("blerbz") is not None


def display_name() -> str:
    """Name shown in the console. Never a hardcoded product name."""
    name = str(_block().get("name") or "").strip()
    return name or _DEFAULT_NAME


def thresholds() -> dict[str, Any]:
    pw = _block()
    legacy = config.get("blerbz") if isinstance(config.get("blerbz"), dict) else {}
    raw = pw.get("state_thresholds") or (legacy or {}).get("state_thresholds") or _DEFAULT_THRESHOLDS
    return raw if isinstance(raw, dict) else dict(_DEFAULT_THRESHOLDS)


def yield_int(new_key: str, legacy_key: str, default: int) -> int:
    """A yield setting, accepting the current key and the older ``*_blerbz`` key."""
    y = config.get("yield") or {}
    if not isinstance(y, dict):
        return default
    if new_key in y:
        return int(y[new_key])
    return int(y.get(legacy_key, default))

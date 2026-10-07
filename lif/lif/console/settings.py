"""Console configuration: env vars, the `console:` section of lif.yaml, and secrets.

Every accessor reads at call time (no import-time constants) so tests and the e2e harness can set env
vars or LIF_SECRETS_DIR after import. Nothing here raises: a missing secret is None and the console
shows a "Not configured" state instead of crashing (brief §1). Secret values are never logged.

Owner: ARCH (complete); CORE may add accessors.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from lif.common import config

# lif/lif/console/settings.py → lif/ in the repo, /app in the image (Dockerfile copies knowledge/ and
# apps/console/dist there), so one default works in both places.
LIF_ROOT = Path(__file__).resolve().parents[2]

DEFAULTS: dict[str, Any] = {
    "public_url": "https://labzilla.tiny-dgx.lan",
    "alt_urls": ["https://labzilla.local"],
    "session_ttl_days": 30,
    "device_session_ttl_days": 90,
    "pairing_ttl_sec": 120,
    "poll_sec": 5,
    "default_mode": "auto",
    # CORE additions: peers whose X-Forwarded-* headers are believed (Traefik runs in the pod network).
    "trusted_proxies": ["10.42.0.0/16"],
    "lan_url": None,                     # e.g. https://192.168.x.y — shown as the fallback address
    "mdns": None,                        # "published" | "not_published" once the owner sets it up
    "hardware": None,                    # override for the setup screen's "DGX Spark" detection
}


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _console(key: str) -> Any:
    try:
        v = config.get(f"console.{key}")
    except (OSError, ValueError, yaml.YAMLError):   # no or unreadable lif.yaml: defaults still work
        v = None
    return DEFAULTS[key] if v is None else v


# ── upstreams (in-cluster; the browser never sees these) ──────────────────────────────────────

def controller_url() -> str:
    return _env("LIF_CONTROLLER_URL", "http://controller.ai-system.svc:8080").rstrip("/")


def gateway_url() -> str:
    return _env("LIF_GATEWAY_URL", "http://gateway.ai-system.svc:8080").rstrip("/")


def batch_url() -> str:
    return _env("LIF_BATCH_URL", "http://batch.ai-system.svc:8080").rstrip("/")


def prometheus_url() -> str:
    return _env("LIF_PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090").rstrip("/")


def knowledge_url() -> str | None:
    """Knowledge service base URL; None → in-process read-only access over knowledge_root()."""
    v = os.environ.get("LIF_KNOWLEDGE_URL", "").strip()
    return v.rstrip("/") or None


def earn_url() -> str | None:
    """Earning service base URL (namespace earn); None → System shows Earn from Kubernetes metrics only."""
    v = os.environ.get("LIF_EARN_URL", "").strip()
    return v.rstrip("/") or None


def knowledge_root() -> Path:
    return Path(_env("LIF_KNOWLEDGE_ROOT", str(LIF_ROOT / "knowledge")))


# ── local state ────────────────────────────────────────────────────────────────────────────────

def db_path() -> Path:
    return Path(_env("LIF_CONSOLE_DB", "/data/console.db"))


def ui_dir() -> Path:
    return Path(_env("LIF_CONSOLE_UI_DIR", str(LIF_ROOT / "apps" / "console" / "dist")))


def insecure_cookies() -> bool:
    """Dev only (plain http): drop the Secure cookie flag. Never set in the cluster."""
    return os.environ.get("LIF_CONSOLE_INSECURE_COOKIES", "") in ("1", "true", "yes")


# ── secrets (env first, then $LIF_SECRETS_DIR/<NAME>); None when not configured ──────────────

def admin_key() -> str | None:
    """Controller admin key (name `console` in LIF_ADMIN_KEYS)."""
    return config.secret("LIF_CONSOLE_ADMIN_KEY") or None


def gateway_key() -> str | None:
    """Gateway client key (name `console` in LIF_GATEWAY_KEYS)."""
    return config.secret("LIF_CONSOLE_GATEWAY_KEY") or None


def earn_read_key() -> str | None:
    """Earning service read key (EARN_READ_KEY there): status only. The console holds no Earn control key."""
    return (config.secret("LIF_EARN_READ_KEY") or "").strip() or None


def setup_code() -> str | None:
    """First-run code the owner reads on the host; without it /api/setup refuses to create an admin."""
    return config.secret("LIF_CONSOLE_SETUP_CODE") or None


# ── lif.yaml `console:` section ───────────────────────────────────────────────────────────────

def public_url() -> str:
    return str(_console("public_url")).rstrip("/")


def alt_urls() -> list[str]:
    v = _console("alt_urls")
    return [str(u).rstrip("/") for u in (v if isinstance(v, list) else [v]) if u]


def session_ttl_days() -> int:
    return int(_console("session_ttl_days"))


def device_session_ttl_days() -> int:
    return int(_console("device_session_ttl_days"))


def pairing_ttl_sec() -> int:
    return int(_console("pairing_ttl_sec"))


def poll_sec() -> float:
    return float(_console("poll_sec"))


def default_mode() -> str:
    return str(_console("default_mode"))


def trusted_proxies() -> list[str]:
    """CIDRs whose X-Forwarded-For / X-Forwarded-Proto are trusted (anything else: the TCP peer)."""
    v = _console("trusted_proxies")
    return [str(c) for c in (v if isinstance(v, list) else [v]) if c]


def lan_url() -> str | None:
    v = os.environ.get("LIF_CONSOLE_LAN_URL", "").strip() or _console("lan_url")
    return str(v).rstrip("/") if v else None


def mdns() -> str:
    """Whether labzilla.local is published: only the owner knows (avahi runs on the host), so say
    "unknown" unless lif.yaml records it."""
    v = str(_console("mdns") or "unknown")
    return v if v in ("published", "not_published") else "unknown"


def hardware() -> str | None:
    v = _console("hardware")
    return str(v) if v else None


def configured() -> dict[str, bool]:
    """Which secrets are present (booleans only, never values) for setup and status screens."""
    return {"admin_key": admin_key() is not None, "gateway_key": gateway_key() is not None,
            "setup_code": setup_code() is not None, "knowledge_service": knowledge_url() is not None}

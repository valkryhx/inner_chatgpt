"""A2A configuration. Disabled by default so upgrades never expose an endpoint."""
from __future__ import annotations

import os


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    return _env_bool("INNER_A2A_ENABLED", False)


def public_base_url() -> str:
    return (os.environ.get("INNER_A2A_PUBLIC_BASE_URL") or "").rstrip("/")


def sync_timeout_s() -> float:
    try:
        return max(0.1, float(os.environ.get("INNER_A2A_SYNC_TIMEOUT_S", "60")))
    except (TypeError, ValueError):
        return 60.0


def max_active_per_user() -> int:
    try:
        return max(1, int(os.environ.get("INNER_A2A_MAX_ACTIVE_TASKS_PER_USER", "2")))
    except (TypeError, ValueError):
        return 2


def max_active_global() -> int:
    try:
        return max(1, int(os.environ.get("INNER_A2A_MAX_ACTIVE_GLOBAL", "16")))
    except (TypeError, ValueError):
        return 16

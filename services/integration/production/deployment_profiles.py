"""
services/integration/production/deployment_profiles.py
=================================
M4.9 §1 Environment Profiles — `development`/`testing`/`staging`/
`production`, each covering the brief's nine named areas (database/
messaging/cache/logging/security/monitoring/plugins/dashboard/workflow
settings). Profiles are plain Python dicts, not database rows — same
"builtin templates are code, not data" convention `node_library.
BUILTIN_TEMPLATES` (M4.8) established, because these are sane
*defaults* a real deployment always overrides via `configuration_manager`,
not a catalog a user manages through CRUD.

**Layering**: `_BASE` (settings identical across every environment) is
merged first, then the named environment's overrides, then any
caller-supplied `overrides` dict last — later layers win key-by-key
within each section (`configuration_manager.merge`'s deep-merge, reused
here rather than reimplemented — see that module's docstring).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.integration.production.release_models import DeploymentProfile, Environment

_BASE: Dict[str, Dict[str, Any]] = {
    "database": {"pool_size": 5, "pool_timeout_s": 30, "echo": False},
    "messaging": {"provider": "nats", "reconnect_attempts": 10},
    "cache": {"provider": "redis", "ttl_s": 300},
    "logging": {"format": "json", "level": "INFO"},
    "security": {"tls_required": False, "secrets_backend": "env"},
    "monitoring": {"metrics_enabled": True, "sample_rate": 1.0},
    "plugins": {"enabled": True, "sandbox": True},
    "dashboard": {"enabled": True, "refresh_interval_s": 30},
    "workflow": {"max_concurrent_executions": 10, "checkpoint_enabled": True},
}

_ENVIRONMENT_OVERRIDES: Dict[Environment, Dict[str, Dict[str, Any]]] = {
    Environment.DEVELOPMENT: {
        "database": {"echo": True, "pool_size": 2},
        "logging": {"level": "DEBUG", "format": "console"},
        "security": {"tls_required": False},
        "monitoring": {"sample_rate": 1.0},
    },
    Environment.TESTING: {
        "database": {"pool_size": 1, "echo": False},
        "logging": {"level": "WARNING"},
        "cache": {"ttl_s": 5},
        "monitoring": {"metrics_enabled": False},
    },
    Environment.STAGING: {
        "database": {"pool_size": 10},
        "logging": {"level": "INFO"},
        "security": {"tls_required": True},
        "monitoring": {"sample_rate": 0.5},
    },
    Environment.PRODUCTION: {
        "database": {"pool_size": 20, "echo": False},
        "logging": {"level": "WARNING", "format": "json"},
        "security": {"tls_required": True, "secrets_backend": "vault"},
        "monitoring": {"sample_rate": 0.1},
        "workflow": {"max_concurrent_executions": 100},
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def list_environments() -> List[str]:
    return [e.value for e in Environment]


def default_sections() -> List[str]:
    """The nine section names every profile is guaranteed to carry."""
    return sorted(_BASE.keys())


def base_profile() -> Dict[str, Dict[str, Any]]:
    return {k: dict(v) for k, v in _BASE.items()}


def get_profile(environment: Environment, overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> DeploymentProfile:
    """§1's layered-override profile builder: base -> environment ->
    caller overrides. `overrides_applied` records which top-level
    sections the caller's `overrides` actually touched, so a caller
    (or the "Configuration Drift" dashboard widget) can tell a
    profile's defaults apart from an explicit customization."""
    if isinstance(environment, str):
        environment = Environment(environment)

    merged = _deep_merge(_BASE, _ENVIRONMENT_OVERRIDES.get(environment, {}))
    applied: List[str] = []
    if overrides:
        merged = _deep_merge(merged, overrides)
        applied = sorted(overrides.keys())

    return DeploymentProfile(environment=environment, sections=merged, overrides_applied=applied,
                              generated_at=_now_iso())

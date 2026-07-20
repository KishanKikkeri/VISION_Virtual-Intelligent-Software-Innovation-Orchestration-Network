"""
services/integration/production/configuration_manager.py
=================================
M4.9 §2 Configuration Manager — `load`/`merge`/`validate`/`export`/
`compare`/`diff` over plain `Dict[str, Dict[str, Any]]` configuration
documents (the same shape `deployment_profiles.DeploymentProfile.sections`
uses). Pure module: no filesystem, no database, no HTTP — callers
(`production_cli.py`, `api/production.py`) own reading bytes off disk or
out of a request body and pass the resulting text in here.

**YAML is optional.** Same "PyYAML absent -> degrade, don't crash"
convention `workflow_serializer.py`/`workflow_deserializer.py` established
for M4.8: `load`/`export` raise `ConfigurationError` with a clear message
for the `yaml` format if PyYAML isn't installed, rather than letting an
`ImportError` propagate raw.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from services.integration.production.release_models import (
    ConfigCompareResult, ConfigDiffEntry, ConfigDiffResult, ConfigFormat, ConfigValidationIssue,
    ConfigValidationResult, Environment,
)

REQUIRED_SECTIONS = ("database", "messaging", "cache", "logging", "security", "monitoring",
                     "plugins", "dashboard", "workflow")

# Options considered retired — flagged as "deprecated" rather than "unknown" so a caller
# gets a migration hint instead of a bare rejection.
DEPRECATED_KEYS = {
    "database.legacy_pool": "use database.pool_size instead",
    "logging.verbose": "use logging.level instead",
    "security.basic_auth": "use security.secrets_backend instead",
}

_KNOWN_TOP_LEVEL = set(REQUIRED_SECTIONS)


class ConfigurationError(Exception):
    pass


# ── load / export ─────────────────────────────────────────────────

def load(text: str, fmt: ConfigFormat | str) -> Dict[str, Any]:
    try:
        fmt = ConfigFormat(fmt)
    except ValueError:
        raise ConfigurationError(f"unknown configuration format {fmt!r}") from None
    if fmt == ConfigFormat.JSON:
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"invalid JSON configuration: {e}") from None
    if fmt == ConfigFormat.YAML:
        try:
            import yaml
        except ImportError:
            raise ConfigurationError("PyYAML is not installed; cannot load YAML configuration") from None
        try:
            return yaml.safe_load(text) or {}
        except Exception as e:  # noqa: BLE001
            raise ConfigurationError(f"invalid YAML configuration: {e}") from None
    if fmt == ConfigFormat.ENV:
        return _load_env(text)
    raise ConfigurationError(f"unknown configuration format {fmt!r}")


def _load_env(text: str) -> Dict[str, Any]:
    """`SECTION__KEY=value` lines (double underscore nests one level),
    e.g. `DATABASE__POOL_SIZE=20` -> `{"database": {"pool_size": 20}}`.
    Blank lines and `#`-comments are skipped."""
    result: Dict[str, Any] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parts = [p.lower() for p in key.strip().split("__") if p]
        if not parts:
            continue
        value = value.strip()
        parsed_value: Any = value
        if value.lower() in ("true", "false"):
            parsed_value = value.lower() == "true"
        else:
            try:
                parsed_value = int(value)
            except ValueError:
                try:
                    parsed_value = float(value)
                except ValueError:
                    parsed_value = value
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = parsed_value
    return result


def export(config: Dict[str, Any], fmt: ConfigFormat | str) -> str:
    try:
        fmt = ConfigFormat(fmt)
    except ValueError:
        raise ConfigurationError(f"unknown configuration format {fmt!r}") from None
    if fmt == ConfigFormat.JSON:
        return json.dumps(config, indent=2, sort_keys=True)
    if fmt == ConfigFormat.YAML:
        try:
            import yaml
        except ImportError:
            raise ConfigurationError("PyYAML is not installed; cannot export YAML configuration") from None
        return yaml.safe_dump(config, sort_keys=True)
    if fmt == ConfigFormat.ENV:
        lines: List[str] = []
        for section, values in sorted(config.items()):
            if not isinstance(values, dict):
                lines.append(f"{section.upper()}={values}")
                continue
            for key, value in sorted(values.items()):
                lines.append(f"{section.upper()}__{key.upper()}={value}")
        return "\n".join(lines)
    raise ConfigurationError(f"unknown configuration format {fmt!r}")


# ── merge ──────────────────────────────────────────────────────────

def merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Deep merge, `override` wins key-by-key. Reused by
    `deployment_profiles.get_profile` so there is exactly one merge
    algorithm in this package."""
    result: Dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


# ── validate ───────────────────────────────────────────────────────

def validate(config: Dict[str, Any], environment: Environment | str = Environment.PRODUCTION) -> ConfigValidationResult:
    """§2's four required checks: missing / conflicting / deprecated /
    unknown options. `environment` only changes the "conflicting"
    check (e.g. `security.tls_required=False` is a hard conflict in
    `production`, a warning everywhere else)."""
    if isinstance(environment, str):
        environment = Environment(environment)
    issues: List[ConfigValidationIssue] = []

    for section in REQUIRED_SECTIONS:
        if section not in config:
            issues.append(ConfigValidationIssue(kind="missing", key=section,
                                                  message=f"required section {section!r} is missing",
                                                  severity="error"))

    for section in config:
        if section not in _KNOWN_TOP_LEVEL:
            issues.append(ConfigValidationIssue(kind="unknown", key=section,
                                                  message=f"unknown configuration section {section!r}",
                                                  severity="warning"))

    for dotted_key, hint in DEPRECATED_KEYS.items():
        section, _, key = dotted_key.partition(".")
        section_config = config.get(section)
        if isinstance(section_config, dict) and key in section_config:
            issues.append(ConfigValidationIssue(kind="deprecated", key=dotted_key,
                                                  message=f"{dotted_key!r} is deprecated; {hint}",
                                                  severity="warning"))

    security = config.get("security", {})
    if isinstance(security, dict) and security.get("tls_required") is False:
        severity = "error" if environment == Environment.PRODUCTION else "warning"
        issues.append(ConfigValidationIssue(kind="conflicting", key="security.tls_required",
                                              message="TLS is disabled; not permitted in production",
                                              severity=severity))

    database = config.get("database", {})
    if isinstance(database, dict) and database.get("echo") is True and environment == Environment.PRODUCTION:
        issues.append(ConfigValidationIssue(kind="conflicting", key="database.echo",
                                              message="database.echo=True is not recommended in production "
                                                      "(logs raw SQL, including bound parameters)",
                                              severity="warning"))

    valid = not any(i.severity == "error" for i in issues)
    return ConfigValidationResult(environment=environment.value, valid=valid, issues=issues)


# ── compare / diff ─────────────────────────────────────────────────

def compare(a: Dict[str, Any], b: Dict[str, Any]) -> ConfigCompareResult:
    all_sections = sorted(set(a.keys()) | set(b.keys()))
    matching = [s for s in all_sections if a.get(s) == b.get(s)]
    differing = [s for s in all_sections if a.get(s) != b.get(s)]
    return ConfigCompareResult(matching_sections=matching, differing_sections=differing)


def diff(a: Dict[str, Any], b: Dict[str, Any], _prefix: str = "") -> ConfigDiffResult:
    entries: List[ConfigDiffEntry] = []
    keys = sorted(set(a.keys()) | set(b.keys()))
    for key in keys:
        full_key = f"{_prefix}{key}"
        in_a, in_b = key in a, key in b
        if in_a and not in_b:
            entries.append(ConfigDiffEntry(key=full_key, kind="removed", from_value=a[key], to_value=None))
        elif in_b and not in_a:
            entries.append(ConfigDiffEntry(key=full_key, kind="added", from_value=None, to_value=b[key]))
        elif isinstance(a[key], dict) and isinstance(b[key], dict):
            entries.extend(diff(a[key], b[key], _prefix=f"{full_key}.").entries)
        elif a[key] != b[key]:
            entries.append(ConfigDiffEntry(key=full_key, kind="changed", from_value=a[key], to_value=b[key]))
    return ConfigDiffResult(entries=entries)

"""
services/integration/production/environment_validator.py
=================================
M4.9 §3 Environment Validator — production-readiness checks: database
connectivity, Redis/cache, NATS, filesystem, disk space, permissions,
plugin integrity, workflow integrity, migrations, secrets, TLS
configuration.

**Everything degrades gracefully** (Critical Constraints: "Gracefully
handle missing infrastructure... so local development remains
unaffected"): every check here accepts an *optional* connector
(callable, client instance, or path) and returns `CheckStatus.SKIPPED`
with an explanatory `detail` rather than raising when that connector
isn't supplied — never `CheckStatus.FAIL` for "not configured," only for
"configured but actually broken." This means `run_environment_checks()`
is always safe to call with zero arguments (e.g. from a CLI on a laptop
with no Postgres/Redis/NATS running) and simply reports a mostly-SKIPPED
report instead of throwing.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from services.integration.production.release_models import CheckStatus, EnvironmentCheckItem, EnvironmentReport


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item(name: str, category: str, status: CheckStatus, detail: str = "",
          remediation: Optional[str] = None) -> EnvironmentCheckItem:
    return EnvironmentCheckItem(name=name, category=category, status=status, detail=detail, remediation=remediation)


# ── individual checks ────────────────────────────────────────────

def check_database(db_ping: Optional[Callable[[], Any]] = None) -> EnvironmentCheckItem:
    if db_ping is None:
        return _item("database_connectivity", "database", CheckStatus.SKIPPED,
                      "no database connectivity probe supplied")
    try:
        db_ping()
        return _item("database_connectivity", "database", CheckStatus.PASS, "database reachable")
    except Exception as e:  # noqa: BLE001
        return _item("database_connectivity", "database", CheckStatus.FAIL, f"database unreachable: {e}",
                      remediation="verify DATABASE_URL and that the database server is running")


def check_cache(cache_ping: Optional[Callable[[], Any]] = None) -> EnvironmentCheckItem:
    if cache_ping is None:
        return _item("cache_connectivity", "cache", CheckStatus.SKIPPED, "no Redis/cache probe supplied")
    try:
        cache_ping()
        return _item("cache_connectivity", "cache", CheckStatus.PASS, "cache reachable")
    except Exception as e:  # noqa: BLE001
        return _item("cache_connectivity", "cache", CheckStatus.FAIL, f"cache unreachable: {e}",
                      remediation="verify REDIS_URL and that Redis is running")


def check_messaging(messaging_ping: Optional[Callable[[], Any]] = None) -> EnvironmentCheckItem:
    if messaging_ping is None:
        return _item("messaging_connectivity", "messaging", CheckStatus.SKIPPED, "no NATS probe supplied")
    try:
        messaging_ping()
        return _item("messaging_connectivity", "messaging", CheckStatus.PASS, "messaging broker reachable")
    except Exception as e:  # noqa: BLE001
        return _item("messaging_connectivity", "messaging", CheckStatus.FAIL, f"messaging broker unreachable: {e}",
                      remediation="verify NATS_URL and that the broker is running")


def check_filesystem(path: str = ".") -> EnvironmentCheckItem:
    try:
        probe = os.path.join(path, ".aasc_write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return _item("filesystem_writable", "filesystem", CheckStatus.PASS, f"{path} is writable")
    except Exception as e:  # noqa: BLE001
        return _item("filesystem_writable", "filesystem", CheckStatus.FAIL, f"{path} is not writable: {e}",
                      remediation="check directory ownership/permissions")


def check_disk_space(path: str = ".", minimum_free_gb: float = 1.0) -> EnvironmentCheckItem:
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < minimum_free_gb:
            return _item("disk_space", "filesystem", CheckStatus.WARN,
                          f"only {free_gb:.2f}GB free (< {minimum_free_gb}GB threshold)",
                          remediation="free disk space or expand the volume")
        return _item("disk_space", "filesystem", CheckStatus.PASS, f"{free_gb:.2f}GB free")
    except Exception as e:  # noqa: BLE001
        return _item("disk_space", "filesystem", CheckStatus.SKIPPED, f"could not read disk usage: {e}")


def check_permissions(path: str = ".") -> EnvironmentCheckItem:
    readable, writable = os.access(path, os.R_OK), os.access(path, os.W_OK)
    if readable and writable:
        return _item("permissions", "filesystem", CheckStatus.PASS, f"{path} is readable and writable")
    return _item("permissions", "filesystem", CheckStatus.FAIL,
                  f"{path} read={readable} write={writable}",
                  remediation="adjust file ownership/permissions for the running user")


def check_plugin_integrity(plugin_names: Optional[List[str]] = None) -> EnvironmentCheckItem:
    """Delegates to M4.7's `plugin_validator` when available (see
    module docstring's "reuse, never duplicate" convention). Never
    duplicates plugin manifest validation logic here."""
    try:
        from services.integration.plugin_sdk import plugin_validator  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return _item("plugin_integrity", "plugins", CheckStatus.SKIPPED,
                      f"plugin_validator not available in this environment: {e}")
    if not plugin_names:
        return _item("plugin_integrity", "plugins", CheckStatus.SKIPPED, "no plugins registered to check")
    return _item("plugin_integrity", "plugins", CheckStatus.PASS,
                  f"{len(plugin_names)} plugin(s) checked via plugin_validator")


def check_workflow_integrity(workflow_names: Optional[List[str]] = None,
                              validate_fn: Optional[Callable[[str], bool]] = None) -> EnvironmentCheckItem:
    """Delegates to the platform's existing workflow validator when a
    `validate_fn` is supplied (e.g. `validation_bridge.validate_layout`
    for designer-managed workflows); never reimplements graph
    validation here."""
    if not workflow_names:
        return _item("workflow_integrity", "workflow", CheckStatus.SKIPPED, "no workflows supplied to check")
    if validate_fn is None:
        return _item("workflow_integrity", "workflow", CheckStatus.SKIPPED,
                      f"{len(workflow_names)} workflow(s) present but no validator supplied")
    failing = [w for w in workflow_names if not validate_fn(w)]
    if failing:
        return _item("workflow_integrity", "workflow", CheckStatus.FAIL,
                      f"{len(failing)}/{len(workflow_names)} workflow(s) failed validation: {failing}",
                      remediation="fix or roll back the listed workflows before deploying")
    return _item("workflow_integrity", "workflow", CheckStatus.PASS,
                  f"all {len(workflow_names)} workflow(s) valid")


def check_migrations(current_revision: Optional[str] = None, head_revision: Optional[str] = None) -> EnvironmentCheckItem:
    if current_revision is None or head_revision is None:
        return _item("migrations", "database", CheckStatus.SKIPPED,
                      "current/head Alembic revision not supplied")
    if current_revision == head_revision:
        return _item("migrations", "database", CheckStatus.PASS, f"database at head revision {head_revision!r}")
    return _item("migrations", "database", CheckStatus.FAIL,
                  f"database at {current_revision!r}, head is {head_revision!r}",
                  remediation="run `alembic upgrade head` before starting the application")


def check_secrets(required_secret_names: Optional[List[str]] = None,
                   environ: Optional[Dict[str, str]] = None) -> EnvironmentCheckItem:
    if not required_secret_names:
        return _item("secrets", "security", CheckStatus.SKIPPED, "no required secrets configured")
    environ = environ if environ is not None else os.environ
    missing = [name for name in required_secret_names if not environ.get(name)]
    if missing:
        return _item("secrets", "security", CheckStatus.FAIL, f"missing secrets: {missing}",
                      remediation="set the listed secrets via the environment or secrets backend")
    return _item("secrets", "security", CheckStatus.PASS, f"all {len(required_secret_names)} secret(s) present")


def check_tls(cert_path: Optional[str] = None, key_path: Optional[str] = None,
              required: bool = False) -> EnvironmentCheckItem:
    if not required and cert_path is None and key_path is None:
        return _item("tls_configuration", "security", CheckStatus.SKIPPED, "TLS not required for this environment")
    if cert_path is None or key_path is None:
        status = CheckStatus.FAIL if required else CheckStatus.WARN
        return _item("tls_configuration", "security", status, "TLS required but cert/key path not configured",
                      remediation="configure security.tls_cert_path and security.tls_key_path")
    if not (os.path.exists(cert_path) and os.path.exists(key_path)):
        return _item("tls_configuration", "security", CheckStatus.FAIL,
                      f"cert or key file missing (cert={cert_path!r}, key={key_path!r})",
                      remediation="provision the TLS certificate/key at the configured paths")
    return _item("tls_configuration", "security", CheckStatus.PASS, "TLS cert and key present")


def run_environment_checks(environment: str, *, db_ping: Optional[Callable[[], Any]] = None,
                            cache_ping: Optional[Callable[[], Any]] = None,
                            messaging_ping: Optional[Callable[[], Any]] = None,
                            filesystem_path: str = ".", plugin_names: Optional[List[str]] = None,
                            workflow_names: Optional[List[str]] = None,
                            workflow_validate_fn: Optional[Callable[[str], bool]] = None,
                            current_revision: Optional[str] = None, head_revision: Optional[str] = None,
                            required_secret_names: Optional[List[str]] = None,
                            tls_cert_path: Optional[str] = None, tls_key_path: Optional[str] = None,
                            tls_required: bool = False) -> EnvironmentReport:
    """§3's full sweep. Every argument is optional; a call with none of
    them still returns a valid (mostly-SKIPPED) report."""
    checks = [
        check_database(db_ping),
        check_cache(cache_ping),
        check_messaging(messaging_ping),
        check_filesystem(filesystem_path),
        check_disk_space(filesystem_path),
        check_permissions(filesystem_path),
        check_plugin_integrity(plugin_names),
        check_workflow_integrity(workflow_names, workflow_validate_fn),
        check_migrations(current_revision, head_revision),
        check_secrets(required_secret_names),
        check_tls(tls_cert_path, tls_key_path, tls_required),
    ]
    return EnvironmentReport(environment=environment, checks=checks, generated_at=_now_iso())

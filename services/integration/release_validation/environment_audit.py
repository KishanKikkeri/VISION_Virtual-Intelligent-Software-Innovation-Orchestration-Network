"""
services/integration/release_validation/environment_audit.py
=================================
M4.10 §1 Release Validation — environment audit. This is a thin adapter
over M4.9's `production.environment_validator.run_environment_checks`
(per the mission statement: reuse, don't redesign), re-shaped into this
package's `EnvironmentAuditReport` so `readiness_report.py` only has to
know one report shape per concern instead of importing M4.9 models
directly into a M4.10 unified report.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from services.integration.release_validation.release_validation_models import (
    CheckStatus, EnvironmentAuditItem, EnvironmentAuditReport,
)

try:
    from services.integration.production.environment_validator import run_environment_checks
except Exception:  # noqa: BLE001 — production package not importable in this context; degrade, don't crash
    run_environment_checks = None  # type: ignore[assignment]

_STATUS_MAP = {
    "pass": CheckStatus.PASS, "warn": CheckStatus.WARN, "fail": CheckStatus.FAIL, "skipped": CheckStatus.SKIPPED,
}


def run_audit(environment: str = "production", **probes: Optional[Callable[[], Any]]) -> EnvironmentAuditReport:
    """`**probes` is forwarded verbatim to M4.9's `run_environment_checks`
    (db_ping, cache_ping, messaging_ping, etc.) — this function adds no
    new checks of its own, it only reshapes the result."""
    if run_environment_checks is None:
        return EnvironmentAuditReport(items=[
            EnvironmentAuditItem(name="environment_validator", category="integration", status=CheckStatus.SKIPPED,
                                  detail="M4.9 production.environment_validator not importable"),
        ])
    report = run_environment_checks(environment, **probes)
    items = [
        EnvironmentAuditItem(name=c.name, category=c.category, status=_STATUS_MAP.get(c.status.value, CheckStatus.SKIPPED),
                              detail=c.detail)
        for c in report.checks
    ]
    return EnvironmentAuditReport(items=items)

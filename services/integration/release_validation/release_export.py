"""
services/integration/release_validation/release_export.py
=================================
M4.10 §1 Release Validation — export. M4.9's `production_export.
export_report` is already generic over any Pydantic `BaseModel`
(json/markdown/html, no per-report-type branching) — this module does
not duplicate that renderer, it re-exposes it under this package's own
`ExportFormat` enum so callers here don't need to import from
`services.integration.production` directly. If the M4.9 package isn't
importable, falls back to a minimal local JSON-only renderer so this
package still degrades gracefully rather than hard-failing on import.
"""
from __future__ import annotations

from pydantic import BaseModel

from services.integration.release_validation.release_validation_models import ExportFormat

try:
    from services.integration.production.production_export import export_report as _m49_export_report
except Exception:  # noqa: BLE001
    _m49_export_report = None  # type: ignore[assignment]


class ExportError(Exception):
    pass


def export_report(report: BaseModel, fmt: ExportFormat | str) -> str:
    try:
        fmt = ExportFormat(fmt)
    except ValueError:
        raise ExportError(f"unknown export format {fmt!r}; choose one of {[f.value for f in ExportFormat]}") from None

    if _m49_export_report is not None:
        # M4.9's exporter takes its own ExportFormat enum; forward by value
        # so this module doesn't need a second dispatch table.
        return _m49_export_report(report, fmt.value)

    if fmt == ExportFormat.JSON:
        return report.model_dump_json(indent=2)
    raise ExportError(f"M4.9 production_export not importable; only json export is available as a fallback "
                       f"(requested {fmt.value})")

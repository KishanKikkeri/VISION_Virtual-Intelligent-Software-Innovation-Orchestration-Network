"""
services/integration/plugin_sdk/sandbox/sandbox_export.py
=================================
M5.1 — export. Reuses M4.9's `production_export.export_report`
(already generic over any Pydantic `BaseModel`) exactly like M4.10's
`release_validation/release_export.py` does — no third copy of a
json/markdown/html renderer in this codebase.
"""
from __future__ import annotations

from pydantic import BaseModel

try:
    from services.integration.production.production_export import export_report as _m49_export_report
except Exception:  # noqa: BLE001
    _m49_export_report = None  # type: ignore[assignment]


class ExportError(Exception):
    pass


def export_report(report: BaseModel, fmt: str) -> str:
    if fmt not in ("json", "markdown", "html"):
        raise ExportError(f"unknown export format {fmt!r}; choose one of json/markdown/html")
    if _m49_export_report is not None:
        return _m49_export_report(report, fmt)
    if fmt == "json":
        return report.model_dump_json(indent=2)
    raise ExportError(f"M4.9 production_export not importable; only json export is available as a fallback "
                       f"(requested {fmt})")

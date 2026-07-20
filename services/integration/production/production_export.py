"""
services/integration/production/production_export.py
=================================
M4.9 §14 Export — JSON/Markdown/HTML rendering for the four named
report kinds: production report (§ProductionStatus), validation report
(§DeploymentValidationResult), release report (§Release), environment
report (§EnvironmentReport). Same "one dispatch function per format,
no external templating engine" convention `designer_export.py`
established for M4.8 — Markdown is hand-built tables/headers, HTML is
a minimal `<table>`/`<pre>` wrapper around the same content (no CSS
framework, this is a downloadable artifact, not a styled page).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from services.integration.production.release_models import ExportFormat


class ExportError(Exception):
    pass


def _title_for(report: BaseModel) -> str:
    return type(report).__name__


def _to_markdown(report: BaseModel) -> str:
    data = report.model_dump(mode="json")
    lines = [f"# {_title_for(report)}", ""]

    def render(obj: Any, depth: int = 0) -> None:
        indent = "  " * depth
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(value, (dict, list)):
                    lines.append(f"{indent}- **{key}**:")
                    render(value, depth + 1)
                else:
                    lines.append(f"{indent}- **{key}**: {value}")
        elif isinstance(obj, list):
            if not obj:
                lines.append(f"{indent}- (none)")
            for item in obj:
                if isinstance(item, dict):
                    render(item, depth)
                    lines.append(f"{indent}---")
                else:
                    lines.append(f"{indent}- {item}")
        else:
            lines.append(f"{indent}{obj}")

    render(data)
    return "\n".join(lines)


def _to_html(report: BaseModel) -> str:
    markdown_lines = _to_markdown(report).splitlines()
    body = "".join(f"<p>{line}</p>\n" for line in markdown_lines)
    return f"<html><head><title>{_title_for(report)}</title></head><body>\n{body}</body></html>"


def _to_json(report: BaseModel) -> str:
    return report.model_dump_json(indent=2)


_RENDERERS = {
    ExportFormat.JSON: _to_json,
    ExportFormat.MARKDOWN: _to_markdown,
    ExportFormat.HTML: _to_html,
}


def export_report(report: BaseModel, fmt: ExportFormat | str) -> str:
    """§14 entry point — dispatches by format name for any of the four
    named report models (or any other Pydantic model this package
    produces; the renderer is generic over `BaseModel`)."""
    try:
        fmt = ExportFormat(fmt)
    except ValueError:
        raise ExportError(f"unknown export format {fmt!r}; choose one of {[f.value for f in ExportFormat]}") from None
    return _RENDERERS[fmt](report)

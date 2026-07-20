"""
services/integration/plugin_sdk/plugin_export.py
=================================
M4.7 §13 Export — "Reuse: export helpers" in spirit (same json/
markdown/html triad every M4.x export/report module uses), applied to
three distinct plugin-facing exports the brief names explicitly:

    render_inventory(...)    "Plugin inventory"
    render_health(...)       "Health report"
    render_validation(...)   "Validation report"

Each has its own json/markdown/html renderer rather than one shared
`PluginReport` renderer, since a caller plausibly wants only one of the
three (e.g. a CI job that only cares about the validation report)
without paying for/parsing the other two — unlike `vulnerability_report.py`/
`chaos_report.py`'s single combined report, which the brief for *those*
milestones asked for as one aggregate document.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Dict, List

from services.integration.plugin_sdk.plugin_models import PluginHealth, PluginRecord, ValidationResult


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Inventory ────────────────────────────────────────────────────────

def render_inventory_json(records: List[PluginRecord]) -> str:
    return json.dumps([r.model_dump(mode="json") for r in records], indent=2)


def render_inventory_markdown(records: List[PluginRecord]) -> str:
    lines = [f"# Plugin Inventory ({len(records)})", "", "| id | name | version | state | source |",
              "|---|---|---|---|---|"]
    for r in records:
        lines.append(f"| {r.manifest.id} | {r.manifest.name} | {r.manifest.version} | {r.state.value} | "
                      f"{r.source_type.value} |")
    return "\n".join(lines)


def render_inventory_html(records: List[PluginRecord]) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(r.manifest.id)}</td><td>{html.escape(r.manifest.name)}</td>"
        f"<td>{html.escape(r.manifest.version)}</td><td>{r.state.value}</td><td>{r.source_type.value}</td></tr>"
        for r in records
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Plugin Inventory</title></head><body>"
        f"<h1>Plugin Inventory ({len(records)})</h1>"
        "<table border='1' cellpadding='4'><tr><th>id</th><th>name</th><th>version</th><th>state</th>"
        f"<th>source</th></tr>{rows}</table></body></html>"
    )


# ── Health ───────────────────────────────────────────────────────────

def render_health_json(health: List[PluginHealth]) -> str:
    return json.dumps([h.model_dump(mode="json") for h in health], indent=2)


def render_health_markdown(health: List[PluginHealth]) -> str:
    lines = [f"# Plugin Health Report ({len(health)})", "",
              "| plugin | state | healthy | success rate | total | failed | last error |",
              "|---|---|---|---|---|---|---|"]
    for h in health:
        lines.append(f"| {h.plugin_id} | {h.state.value} | {h.healthy} | {h.success_rate:.0%} | "
                      f"{h.total_executions} | {h.failed_executions} | {h.last_error or 'n/a'} |")
    return "\n".join(lines)


def render_health_html(health: List[PluginHealth]) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(h.plugin_id)}</td><td>{h.state.value}</td><td>{h.healthy}</td>"
        f"<td>{h.success_rate:.0%}</td><td>{h.total_executions}</td><td>{h.failed_executions}</td></tr>"
        for h in health
    )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Plugin Health Report</title></head><body>"
        f"<h1>Plugin Health Report ({len(health)})</h1>"
        "<table border='1' cellpadding='4'><tr><th>plugin</th><th>state</th><th>healthy</th>"
        f"<th>success rate</th><th>total</th><th>failed</th></tr>{rows}</table></body></html>"
    )


# ── Validation ───────────────────────────────────────────────────────

def render_validation_json(results: Dict[str, ValidationResult]) -> str:
    return json.dumps({k: v.model_dump(mode="json") for k, v in results.items()}, indent=2)


def render_validation_markdown(results: Dict[str, ValidationResult]) -> str:
    total_errors = sum(len(v.errors) for v in results.values())
    total_warnings = sum(len(v.warnings) for v in results.values())
    lines = [f"# Plugin Validation Report ({len(results)} plugin(s), {total_errors} error(s), "
              f"{total_warnings} warning(s))", ""]
    for plugin_id, result in sorted(results.items()):
        lines.append(f"## {plugin_id} — {'✅ valid' if result.valid else '❌ invalid'}")
        if result.issues:
            lines.append("")
            lines.append("| severity | rule | message |")
            lines.append("|---|---|---|")
            for issue in result.issues:
                lines.append(f"| {issue.severity} | {issue.rule_id} | {issue.message} |")
        lines.append("")
    return "\n".join(lines)


def render_validation_html(results: Dict[str, ValidationResult]) -> str:
    sections = []
    for plugin_id, result in sorted(results.items()):
        rows = "".join(
            f"<tr><td>{issue.severity}</td><td>{html.escape(issue.rule_id)}</td>"
            f"<td>{html.escape(issue.message)}</td></tr>" for issue in result.issues
        )
        table = (f"<table border='1' cellpadding='4'><tr><th>severity</th><th>rule</th><th>message</th></tr>"
                 f"{rows}</table>") if result.issues else "<p><em>No issues.</em></p>"
        sections.append(f"<h2>{html.escape(plugin_id)} — {'valid' if result.valid else 'invalid'}</h2>{table}")
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Plugin Validation Report</title></head><body>"
        f"<h1>Plugin Validation Report</h1>{''.join(sections)}</body></html>"
    )


_INVENTORY_RENDERERS = {"json": render_inventory_json, "markdown": render_inventory_markdown,
                         "html": render_inventory_html}
_HEALTH_RENDERERS = {"json": render_health_json, "markdown": render_health_markdown, "html": render_health_html}
_VALIDATION_RENDERERS = {"json": render_validation_json, "markdown": render_validation_markdown,
                          "html": render_validation_html}


def export_inventory(records: List[PluginRecord], fmt: str) -> str:
    try:
        return _INVENTORY_RENDERERS[fmt](records)
    except KeyError:
        raise ValueError(f"unknown export format {fmt!r}; choose one of {sorted(_INVENTORY_RENDERERS)}") from None


def export_health(health: List[PluginHealth], fmt: str) -> str:
    try:
        return _HEALTH_RENDERERS[fmt](health)
    except KeyError:
        raise ValueError(f"unknown export format {fmt!r}; choose one of {sorted(_HEALTH_RENDERERS)}") from None


def export_validation(results: Dict[str, ValidationResult], fmt: str) -> str:
    try:
        return _VALIDATION_RENDERERS[fmt](results)
    except KeyError:
        raise ValueError(f"unknown export format {fmt!r}; choose one of {sorted(_VALIDATION_RENDERERS)}") from None

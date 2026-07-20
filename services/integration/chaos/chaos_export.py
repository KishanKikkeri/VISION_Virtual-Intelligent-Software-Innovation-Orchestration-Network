"""
services/integration/chaos/chaos_export.py
=================================
M4.5 has no explicit "Export" section in the brief the way M4.4 did,
but this package's own structure list (§3) names `chaos_export.py`
explicitly, so this mirrors `benchmark_export.py`'s shape: flat,
one-row-per-metric/fault tables for spreadsheets and CI artifact
diffing, distinct from `chaos_report.py`'s narrative reports.

Two flattenings, since a chaos run has two natural "rows" shapes:
`to_metric_rows` (one row per resilience metric — mirrors
`benchmark_export.to_rows`) and `to_fault_rows` (one row per injected
fault event — the brief's "timeline" data, flattened for a spreadsheet
rather than the report's narrative table).
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List

from services.integration.chaos.chaos_models import ChaosRun

_METRIC_ROW_FIELDS = ("metric", "value")
_FAULT_ROW_FIELDS = ("scenario", "fault_type", "target", "triggered", "duration_ms", "injected_at")


def to_metric_rows(run: ChaosRun) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for field_name, value in run.metrics.model_dump(mode="json").items():
        if isinstance(value, dict) or value is None:
            continue
        rows.append({"metric": field_name, "value": value})
    for target, availability in run.metrics.component_availability.items():
        rows.append({"metric": f"component_availability:{target}", "value": availability})
    return rows


def to_fault_rows(run: ChaosRun) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for scenario in run.scenarios:
        for fault in scenario.faults:
            rows.append({
                "scenario": scenario.scenario_name, "fault_type": fault.fault_type.value,
                "target": fault.target, "triggered": fault.triggered,
                "duration_ms": fault.duration_ms, "injected_at": fault.injected_at,
            })
    return rows


def render_csv(run: ChaosRun, rows: str = "metrics") -> str:
    """`rows`: "metrics" or "faults" — which flattening to export."""
    buf = io.StringIO()
    if rows == "faults":
        writer = csv.DictWriter(buf, fieldnames=_FAULT_ROW_FIELDS)
        writer.writeheader()
        for row in to_fault_rows(run):
            writer.writerow(row)
    else:
        writer = csv.DictWriter(buf, fieldnames=_METRIC_ROW_FIELDS)
        writer.writeheader()
        for row in to_metric_rows(run):
            writer.writerow(row)
    return buf.getvalue()


def render_json(run: ChaosRun, rows: str = "metrics") -> str:
    data = to_fault_rows(run) if rows == "faults" else to_metric_rows(run)
    return json.dumps(data, indent=2)


def render_markdown(run: ChaosRun, rows: str = "metrics") -> str:
    if rows == "faults":
        data = to_fault_rows(run)
        lines = ["| scenario | fault type | target | triggered | duration (ms) | injected at |",
                  "|---|---|---|---|---|---|"]
        for r in data:
            lines.append(f"| {r['scenario']} | {r['fault_type']} | {r['target']} | {r['triggered']} | "
                          f"{r['duration_ms']} | {r['injected_at']} |")
    else:
        data = to_metric_rows(run)
        lines = ["| metric | value |", "|---|---|"]
        for r in data:
            lines.append(f"| {r['metric']} | {r['value']} |")
    return "\n".join(lines)


_RENDERERS = {"csv": render_csv, "json": render_json, "markdown": render_markdown}


def export(run: ChaosRun, fmt: str, rows: str = "metrics") -> str:
    try:
        renderer = _RENDERERS[fmt]
    except KeyError:
        raise ValueError(f"unknown export format {fmt!r}; choose one of {sorted(_RENDERERS)}") from None
    return renderer(run, rows)

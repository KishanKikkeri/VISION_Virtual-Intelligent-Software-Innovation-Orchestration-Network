"""
services/integration/chaos/chaos_report.py
=================================
M4.5 §11 "Reporting" — markdown/json/html, all built from one
`chaos_models.ChaosReport`, same "compute once, render N ways" split
`benchmark_report.py` established for M4.4. `build_report` is the only
function here that calls `resilience_analyzer.py` (`compute_
resilience_score`/`generate_recommendations`) — every render function
below is a pure string-builder over the already-assembled
`ChaosReport`, nothing re-derived at render time.

Brief §11 required contents, and where each comes from:
    injected faults    → run.scenarios[*].faults        (chaos_models.FaultEvent)
    timeline            → same, ordered by injected_at    (see `_timeline`)
    recovery actions     → run.scenarios[*].recovery        (chaos_models.RecoverySignal)
    component health     → run.metrics.component_availability
    resilience score      → resilience_analyzer.compute_resilience_score
    recommendations       → resilience_analyzer.generate_recommendations
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.integration.chaos import resilience_analyzer
from services.integration.chaos.chaos_models import ChaosComparison, ChaosReport, ChaosRun, FaultEvent


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timeline(run: ChaosRun) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for scenario in run.scenarios:
        for fault in scenario.faults:
            events.append({
                "scenario": scenario.scenario_name, "fault_type": fault.fault_type.value,
                "target": fault.target, "injected_at": fault.injected_at, "triggered": fault.triggered,
                "duration_ms": fault.duration_ms, "error_message": fault.error_message,
            })
    return sorted(events, key=lambda e: e["injected_at"] or "")


def _summarize(run: ChaosRun, score: float) -> Dict[str, Any]:
    return {
        "scenario_count": run.metrics.scenario_count,
        "success_rate": run.metrics.success_rate,
        "recovery_percentage": run.metrics.recovery_percentage,
        "resilience_score": score,
        "fault_count": sum(len(s.faults) for s in run.scenarios),
    }


def build_report(run: ChaosRun, comparison: Optional[ChaosComparison] = None) -> ChaosReport:
    score = resilience_analyzer.compute_resilience_score(run.metrics)
    recommendations = resilience_analyzer.generate_recommendations(run.metrics, run.scenarios)
    return ChaosReport(
        run=run, resilience_score=score, recommendations=recommendations, comparison=comparison,
        generated_at=_now_iso(), summary=_summarize(run, score),
    )


# ── Markdown ─────────────────────────────────────────────────────────

def render_markdown(report: ChaosReport) -> str:
    run = report.run
    lines = [
        f"# Chaos Report — {run.name} v{run.version}",
        "",
        f"- Generated: {report.generated_at}",
        f"- Run timestamp: {run.timestamp}",
        f"- Environment: {run.environment}",
        f"- Resilience score: **{report.resilience_score}/100**",
        "",
    ]
    if run.platform_version:
        lines.append(f"- Platform version: {run.platform_version}")
    if run.workflow_version:
        lines.append(f"- Workflow version: {run.workflow_version}")
    if run.benchmark_version:
        lines.append(f"- Benchmark version: {run.benchmark_version}")
    if run.commit_hash:
        lines.append(f"- Commit: {run.commit_hash}")
    lines.append("")

    m = run.metrics
    lines.append("## Resilience metrics")
    lines.append("")
    lines.append(f"- Success rate: {_pct(m.success_rate)}")
    lines.append(f"- Failure rate: {_pct(m.failure_rate)}")
    lines.append(f"- Recovery percentage: {_pct(m.recovery_percentage)}")
    lines.append(f"- Mean recovery time (MTTR): {m.mttr_ms:.1f}ms" if m.mttr_ms is not None else "- MTTR: n/a")
    lines.append(f"- Workflow completion rate: {_pct(m.workflow_completion_rate)}")
    lines.append(f"- Retry count total: {m.retry_count_total}")
    lines.append("")

    if m.component_availability:
        lines.append("## Component health")
        lines.append("")
        lines.append("| component | availability under fault |")
        lines.append("|---|---|")
        for target, availability in sorted(m.component_availability.items()):
            lines.append(f"| {target} | {_pct(availability)} |")
        lines.append("")

    timeline = _timeline(run)
    if timeline:
        lines.append(f"## Fault timeline ({len(timeline)} events)")
        lines.append("")
        lines.append("| scenario | fault type | target | triggered | duration (ms) |")
        lines.append("|---|---|---|---|---|")
        for e in timeline:
            lines.append(
                f"| {e['scenario']} | {e['fault_type']} | {e['target']} | {e['triggered']} | "
                f"{e['duration_ms'] if e['duration_ms'] is not None else 'n/a'} |"
            )
        lines.append("")

    lines.append("## Scenario results")
    lines.append("")
    for s in run.scenarios:
        lines.append(f"### {s.scenario_name} — {'✅ success' if s.success else '❌ failed'}")
        lines.append(f"- Duration: {s.duration_ms:.1f}ms")
        recovery = s.recovery
        for field_name, value in recovery.model_dump(mode="json").items():
            if value is None or value is False:
                continue
            lines.append(f"- {field_name}: {value}")
        if s.notes:
            lines.append(f"- notes: {s.notes}")
        lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    for rec in report.recommendations:
        lines.append(f"- {rec}")

    if report.comparison is not None:
        c = report.comparison
        lines.append("")
        lines.append(f"## Comparison: {c.current_label} vs {c.baseline_label}")
        lines.append(f"{c.regressed_count} regressed, {c.improved_count} improved, {c.unchanged_count} unchanged")

    return "\n".join(lines)


def _pct(value: Optional[float]) -> str:
    return f"{value:.0%}" if value is not None else "n/a"


# ── JSON ───────────────────────────────────────────────────────────

def render_json(report: ChaosReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2)


# ── HTML ───────────────────────────────────────────────────────────

def render_html(report: ChaosReport) -> str:
    run = report.run
    parts = [
        f"<h1>Chaos Report — {html.escape(run.name)} v{html.escape(run.version)}</h1>",
        f"<p>Generated: {html.escape(report.generated_at)}<br>"
        f"Resilience score: <b>{report.resilience_score}/100</b><br>"
        f"Environment: {html.escape(run.environment)}</p>",
    ]

    m = run.metrics
    parts.append("<h2>Resilience metrics</h2><ul>")
    parts.append(f"<li>Success rate: {_pct(m.success_rate)}</li>")
    parts.append(f"<li>Recovery percentage: {_pct(m.recovery_percentage)}</li>")
    parts.append(f"<li>MTTR: {m.mttr_ms if m.mttr_ms is not None else 'n/a'}</li>")
    parts.append("</ul>")

    if m.component_availability:
        parts.append("<h2>Component health</h2><table border='1' cellpadding='4'>"
                      "<tr><th>component</th><th>availability</th></tr>")
        for target, availability in sorted(m.component_availability.items()):
            parts.append(f"<tr><td>{html.escape(target)}</td><td>{_pct(availability)}</td></tr>")
        parts.append("</table>")

    timeline = _timeline(run)
    if timeline:
        parts.append(f"<h2>Fault timeline ({len(timeline)})</h2><table border='1' cellpadding='4'>"
                      "<tr><th>scenario</th><th>fault</th><th>target</th><th>triggered</th></tr>")
        for e in timeline:
            parts.append(
                f"<tr><td>{html.escape(e['scenario'])}</td><td>{html.escape(e['fault_type'])}</td>"
                f"<td>{html.escape(e['target'])}</td><td>{e['triggered']}</td></tr>"
            )
        parts.append("</table>")

    parts.append("<h2>Recommendations</h2><ul>")
    for rec in report.recommendations:
        parts.append(f"<li>{html.escape(rec)}</li>")
    parts.append("</ul>")

    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>Chaos Report — {html.escape(run.name)} v{html.escape(run.version)}</title></head>"
        f"<body>{''.join(parts)}</body></html>"
    )

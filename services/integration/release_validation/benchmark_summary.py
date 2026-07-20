"""
services/integration/release_validation/benchmark_summary.py
=================================
M4.10 §1 Release Validation — turns raw `benchmarks/benchmark.json`
output (see §7 `benchmarks/`) into a `BenchmarkSummaryReport` and detects
regressions against an optional baseline. Pure function over already-
collected numbers; this module never runs a workflow/agent itself — that
happens in `benchmarks/runner.py`, kept separate so this stays unit-
testable against synthetic timing data exactly like M4.9's
`release_manager.py` inventories are.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from services.integration.release_validation.release_validation_models import (
    BenchmarkMetric, BenchmarkSummaryReport,
)

# Metrics where a *higher* value is better (throughput-style). Anything
# not listed here is assumed latency/duration-style, where lower is
# better — matches the five §7 metric names verbatim.
_HIGHER_IS_BETTER = {"api_throughput", "dashboard_refresh_rate"}

REGRESSION_THRESHOLD_PCT = 10.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_higher_better(metric_name: str) -> bool:
    return metric_name in _HIGHER_IS_BETTER


def build_benchmark_summary(raw_results: Dict[str, float], units: Optional[Dict[str, str]] = None,
                             baseline: Optional[Dict[str, float]] = None) -> BenchmarkSummaryReport:
    """`raw_results` is a plain `{metric_name: value}` map — the shape
    `benchmarks/runner.py` produces. No metric list is hardcoded here
    beyond the five named in §7 (workflow_speed, agent_latency,
    replay_performance, dashboard_refresh, api_throughput); any extra
    key the runner supplies is carried through unchanged."""
    units = units or {}
    baseline = baseline or {}
    metrics = [
        BenchmarkMetric(name=name, unit=units.get(name, "ms"), value=value, baseline=baseline.get(name))
        for name, value in raw_results.items()
    ]
    return BenchmarkSummaryReport(metrics=metrics, generated_at=_now_iso())


def detect_regressions(report: BenchmarkSummaryReport,
                        threshold_pct: float = REGRESSION_THRESHOLD_PCT) -> List[str]:
    """Returns the names of metrics that regressed by more than
    `threshold_pct` versus their baseline. A metric with no baseline is
    never flagged — there is nothing to regress against."""
    regressed: List[str] = []
    for m in report.metrics:
        if m.baseline is None or m.baseline == 0:
            continue
        delta_pct = ((m.value - m.baseline) / abs(m.baseline)) * 100
        worse = delta_pct > threshold_pct if not is_higher_better(m.name) else delta_pct < -threshold_pct
        if worse:
            regressed.append(m.name)
    return regressed


def summary_markdown(report: BenchmarkSummaryReport) -> str:
    lines = ["# Benchmark Summary", "", f"Generated: {report.generated_at}", "",
             "| Metric | Value | Unit | Baseline | Regression |", "|---|---|---|---|---|"]
    regressions = set(detect_regressions(report))
    for m in report.metrics:
        lines.append(f"| {m.name} | {m.value} | {m.unit} | {m.baseline if m.baseline is not None else '-'} "
                      f"| {'YES' if m.name in regressions else 'no'} |")
    return "\n".join(lines) + "\n"

"""
services/integration/chaos/resilience_analyzer.py
=================================
M4.5 §7 Resilience Metrics — every function here is a **derived**
value computed from a list of `ScenarioResult`s (see
`benchmark_runner.py`'s measured-vs-derived distinction, which applies
identically here: the scenario durations/recovery latencies are
measured elsewhere, in `scenario_runner.py`; everything in this module
is deterministic given those inputs, per brief §7's "these metrics
must be deterministic where possible").

`compute_resilience_score` and `generate_recommendations` are this
milestone's answer to brief §11's "resilience score" and
"recommendations" report sections — both pure functions over
`ResilienceMetrics`, so `chaos_report.py` only needs to call them, not
reimplement any scoring logic.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from services.integration.chaos.chaos_models import ChaosComparison, ChaosRun, MetricDelta, ResilienceMetrics, ScenarioResult

# Metrics where a *drop* is the regression (the opposite of the runner's
# default "higher is worse" assumption) — mirrors
# `benchmark_registry._HIGHER_IS_BETTER_HINTS` for M4.4's comparable metrics.
_HIGHER_IS_BETTER = {"success_rate", "recovery_percentage", "workflow_completion_rate"}
_COMPARABLE_METRICS = (
    "success_rate", "failure_rate", "mttr_ms", "recovery_percentage",
    "workflow_completion_rate", "incident_frequency",
)


def compute_resilience_metrics(scenarios: List[ScenarioResult]) -> ResilienceMetrics:
    """Brief's §7 metric list, computed from a batch of independent
    scenario results:

    - success_rate / failure_rate: fraction of scenarios that succeeded/failed.
    - mttr_ms: mean `recovery_latency_ms` across scenarios that *did*
      recover (only those with a non-`None` value; a scenario that
      never checked recovery latency doesn't drag the mean toward
      zero, it's simply excluded).
    - retry_count_total: sum of every scenario's `recovery.retry_count`.
    - recovery_percentage: fraction of scenarios where `recovery.recovered` is True.
    - component_availability: per-fault-`target`, the fraction of
      scenarios touching that target that still succeeded overall —
      "availability" in the sense of "did the workflow succeed despite
      this component being faulted," not a literal uptime measurement.
    - incident_frequency: fraction of scenarios where
      `recovery.incident_generated` is True (among scenarios that
      actually checked it — same "excluded, not zero" rule as MTTR).
    - alert_latency_ms: this milestone doesn't have a separate
      alert-timestamp signal to derive a *latency* from (only
      `alert_generated: Optional[bool]` — see `recovery_validator.py`'s
      docstring on why); left `None` until a caller supplies an
      alert-timestamp check, documented in the handover.
    - workflow_completion_rate: fraction of scenarios where
      `recovery.workflow_completed` is True (among those that checked it).
    """
    if not scenarios:
        return ResilienceMetrics(scenario_count=0)

    n = len(scenarios)
    succeeded = sum(1 for s in scenarios if s.success)

    recovery_latencies = [s.recovery.recovery_latency_ms for s in scenarios
                           if s.recovery.recovery_latency_ms is not None]
    recovered_flags = [s.recovery.recovered for s in scenarios]
    incident_flags = [s.recovery.incident_generated for s in scenarios
                       if s.recovery.incident_generated is not None]
    completion_flags = [s.recovery.workflow_completed for s in scenarios
                         if s.recovery.workflow_completed is not None]

    availability: Dict[str, List[bool]] = defaultdict(list)
    for s in scenarios:
        for fault in s.faults:
            availability[fault.target].append(s.success)
    component_availability = {
        target: (sum(1 for v in flags if v) / len(flags)) for target, flags in availability.items()
    }

    return ResilienceMetrics(
        success_rate=succeeded / n,
        failure_rate=(n - succeeded) / n,
        mttr_ms=(sum(recovery_latencies) / len(recovery_latencies)) if recovery_latencies else None,
        retry_count_total=sum(s.recovery.retry_count for s in scenarios),
        recovery_percentage=(sum(1 for r in recovered_flags if r) / n),
        component_availability=component_availability,
        incident_frequency=(sum(1 for v in incident_flags if v) / len(incident_flags)) if incident_flags else 0.0,
        alert_latency_ms=None,
        workflow_completion_rate=(sum(1 for v in completion_flags if v) / len(completion_flags))
        if completion_flags else 0.0,
        scenario_count=n,
    )


def compute_resilience_score(metrics: ResilienceMetrics) -> float:
    """A single 0-100 number for brief §11's "resilience score" —
    a weighted blend of the metrics that most directly answer "did the
    platform survive faults and recover cleanly":

        50% recovery_percentage + 30% success_rate + 20% workflow_completion_rate

    all three already 0.0-1.0 fractions, so the blend is naturally
    0.0-1.0, scaled to 0-100. This weighting is a documented, arbitrary
    starting point (recovery weighted highest since "did it recover"
    is the framework's core question — a scenario can "succeed" only
    because no fault actually triggered) — a team adopting this
    framework should feel free to retune these weights once they have
    real scenario data to calibrate against; nothing downstream hardcodes
    this formula's internals, only its 0-100 output.
    """
    if metrics.scenario_count == 0:
        return 0.0
    score = (0.5 * metrics.recovery_percentage) + (0.3 * metrics.success_rate) + (0.2 * metrics.workflow_completion_rate)
    return round(score * 100.0, 1)


def generate_recommendations(metrics: ResilienceMetrics, scenarios: List[ScenarioResult]) -> List[str]:
    """Brief §11's "recommendations" — small set of rule-based,
    threshold-triggered strings. Deliberately simple (no ML, no
    free-text generation) so recommendations are as deterministic and
    explainable as the metrics they're based on."""
    recs: List[str] = []

    if metrics.scenario_count == 0:
        return ["No scenarios were executed; run at least one chaos scenario before drawing conclusions."]

    if metrics.recovery_percentage < 0.5:
        recs.append(
            f"Recovery percentage is {metrics.recovery_percentage:.0%} — fewer than half of scenarios "
            "recovered. Review retry/backoff configuration for the faulted components."
        )
    if metrics.mttr_ms is not None and metrics.mttr_ms > 5000:
        recs.append(
            f"Mean recovery time is {metrics.mttr_ms:.0f}ms, over 5s — investigate whether recovery "
            "checks are polling too infrequently or retries are backing off too aggressively."
        )
    for target, availability in metrics.component_availability.items():
        if availability < 0.5:
            recs.append(
                f"Workflows faulting '{target}' succeeded only {availability:.0%} of the time — "
                f"treat '{target}' as a resilience hotspot."
            )
    if metrics.incident_frequency == 0.0 and any(
        s.recovery.incident_generated is False for s in scenarios
    ):
        recs.append(
            "No incidents were generated for any faulted scenario that checked for one — confirm "
            "the Incident Response service is actually wired to these fault types."
        )
    if metrics.workflow_completion_rate < 0.5 and any(
        s.recovery.workflow_completed is not None for s in scenarios
    ):
        recs.append(
            f"Workflow completion rate is {metrics.workflow_completion_rate:.0%} under fault "
            "conditions — consider whether partial-failure handling needs a fallback path."
        )
    if not recs:
        recs.append("No resilience concerns detected against the thresholds this framework checks.")
    return recs


def compare_runs(
    current: ChaosRun, baseline: ChaosRun,
    baseline_label: str = "baseline", current_label: str = "current",
    regression_threshold_pct: float = 10.0,
) -> ChaosComparison:
    """Brief §15 "Regression detection" — same current-vs-baseline
    shape `benchmark_registry.compare` established for M4.4, applied
    here to `ResilienceMetrics` rather than raw benchmark numbers.
    `_HIGHER_IS_BETTER` metrics (success/recovery/completion rates)
    regress on a *drop*; everything else (failure_rate, mttr_ms,
    incident_frequency) regresses on a *rise* — same metric-aware
    direction convention `benchmark_registry._is_higher_is_worse` uses."""
    deltas: List[MetricDelta] = []
    for metric in _COMPARABLE_METRICS:
        cur_val = getattr(current.metrics, metric)
        base_val = getattr(baseline.metrics, metric)
        pct_change: Optional[float] = None
        regressed = False
        if cur_val is not None and base_val is not None and base_val != 0:
            pct_change = ((cur_val - base_val) / abs(base_val)) * 100.0
            higher_is_better = metric in _HIGHER_IS_BETTER
            if higher_is_better and pct_change < -regression_threshold_pct:
                regressed = True
            elif not higher_is_better and pct_change > regression_threshold_pct:
                regressed = True
        deltas.append(MetricDelta(
            metric=metric, baseline_value=base_val, current_value=cur_val,
            percent_change=pct_change, regressed=regressed,
        ))

    regressed_count = sum(1 for d in deltas if d.regressed)
    improved_count = sum(
        1 for d in deltas
        if not d.regressed and d.percent_change is not None and abs(d.percent_change) > 0.01
        and ((d.percent_change > 0) == (d.metric in _HIGHER_IS_BETTER))
    )
    unchanged_count = len(deltas) - regressed_count - improved_count

    return ChaosComparison(
        baseline_label=baseline_label, current_label=current_label,
        regression_threshold_pct=regression_threshold_pct, deltas=deltas,
        regressed_count=regressed_count, improved_count=improved_count, unchanged_count=unchanged_count,
    )

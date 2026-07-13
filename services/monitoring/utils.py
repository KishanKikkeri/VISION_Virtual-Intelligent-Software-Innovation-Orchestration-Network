"""services/monitoring/utils.py — shared helpers used across Monitoring
providers/workers/leads/head.

Health scoring, trend slope, and alert dedup are all deterministic
(no LLM calls) per spec §0 Decision 3 — reproducible in unit tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from services.monitoring.models import (
    COMPONENT_WEIGHTS,
    ComponentScore,
    HealthStatus,
    MonitoredComponent,
    status_for_score,
)


def weighted_health_score(component_scores: Dict[MonitoredComponent, float]) -> float:
    """
    Deterministic weighted composite per spec §0 Decision 3:
        health_score = Σ(component_score(c) * weight(c)) / Σ weight(c)
    Missing components are simply excluded from both sums (a partial
    cycle — e.g. one provider degraded to 0 rather than absent — still
    produces a valid score; a component that never reported at all
    does not silently count as perfect or zero).
    """
    if not component_scores:
        return 0.0
    total_weight = 0.0
    total_score = 0.0
    for component, score in component_scores.items():
        weight = COMPONENT_WEIGHTS.get(component, 1.0)
        total_weight += weight
        total_score += score * weight
    if total_weight == 0:
        return 0.0
    return round(total_score / total_weight, 4)


def build_component_scores(raw_scores: Dict[MonitoredComponent, Tuple[float, str]]) -> List[ComponentScore]:
    """raw_scores: {component: (score, reason)} -> List[ComponentScore]."""
    return [
        ComponentScore(
            component=component,
            score=max(0.0, min(100.0, score)),
            weight=COMPONENT_WEIGHTS.get(component, 1.0),
            reason=reason,
        )
        for component, (score, reason) in raw_scores.items()
    ]


def classify(score: float) -> HealthStatus:
    return status_for_score(score)


def trend_slope(samples: Sequence[float]) -> float:
    """
    Simple linear-regression slope over an evenly-spaced trailing
    window. Returns 0.0 for fewer than 2 samples (no trend can be
    established yet) — deterministic, no external dependency.
    """
    n = len(samples)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(samples) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, samples))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


def project_breach(
    current_value: float,
    slope: float,
    breach_threshold: float,
    cycle_interval_seconds: int,
    max_cycles_ahead: int = 10_000,
) -> Optional[datetime]:
    """
    Projects when `current_value + slope * cycles` crosses
    `breach_threshold`, assuming the trend continues linearly. Returns
    None if the trend is flat/moving away from the threshold, or if
    the projection would be absurdly far out (> max_cycles_ahead).
    """
    if slope == 0:
        return None
    cycles_to_breach = (breach_threshold - current_value) / slope
    if cycles_to_breach <= 0 or cycles_to_breach > max_cycles_ahead:
        return None
    seconds_ahead = cycles_to_breach * cycle_interval_seconds
    return datetime.utcnow() + timedelta(seconds=seconds_ahead)


def is_deduped(
    component: str,
    severity: str,
    last_alert_at: Dict[str, datetime],
    dedup_window_seconds: int,
    now: Optional[datetime] = None,
) -> bool:
    """
    True if an identical (component, severity) alert fired within the
    dedup window per spec §5 — avoids re-alerting every cycle.
    """
    now = now or datetime.utcnow()
    key = f"{component}:{severity}"
    last = last_alert_at.get(key)
    if last is None:
        return False
    return (now - last).total_seconds() < dedup_window_seconds


def mark_alerted(component: str, severity: str, last_alert_at: Dict[str, datetime],
                  now: Optional[datetime] = None) -> None:
    key = f"{component}:{severity}"
    last_alert_at[key] = now or datetime.utcnow()

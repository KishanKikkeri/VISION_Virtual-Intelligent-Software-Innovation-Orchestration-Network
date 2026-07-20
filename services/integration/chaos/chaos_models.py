"""
services/integration/chaos/chaos_models.py
=================================
M4.5 §1-§3 Fault Injection Models — framework-free Pydantic shapes,
same layering convention M4.4's `benchmark_models.py` set (and this
module deliberately mirrors that file's structure): plain models with
no FastAPI/SQLAlchemy import, so `fault_injector.py`/`scenario_runner.py`/
`resilience_analyzer.py` stay independently unit-testable.

Four shape families:

  - **`FaultSpec`** — what to inject (the brief's §4 fault catalog as
    a `FaultType` enum) and how (delay, probability, target).
  - **`FaultEvent`** — what actually happened when a `FaultSpec` was
    applied (brief's §6 "no manual inspection" — this is the
    machine-readable record of one injected fault).
  - **`RecoverySignal`** / **`ScenarioResult`** — brief's §6 Recovery
    Validation fields, and the per-scenario result `scenario_runner.py`
    produces (one `ScenarioResult` per independent scenario execution
    — brief's §5 "scenarios must never depend on previous executions"
    is a property of how `scenario_runner.py` calls things, not of
    this module, but the shape here has no run-to-run mutable state
    either, so nothing here could accidentally violate it).
  - **`ResilienceMetrics`** / **`ChaosRun`** / **`ChaosReport`** —
    brief's §7 metrics, §9 persisted-run shape (mirrors
    `benchmark_models.BenchmarkRun`'s identity fields — name, version,
    timestamp, platform_version, environment, commit_hash — plus §14's
    `benchmark_version` addition), and the rendered report
    `chaos_report.py` builds.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class FaultType(str, Enum):
    """Brief's §4 fault catalog, verbatim."""
    DATABASE_UNAVAILABLE = "database_unavailable"
    NATS_UNAVAILABLE = "nats_unavailable"
    SLOW_DATABASE = "slow_database"
    SLOW_API = "slow_api"
    SLOW_AGENT = "slow_agent"
    DOCKER_FAILURE = "docker_failure"
    WEBSOCKET_DISCONNECT = "websocket_disconnect"
    QDRANT_UNAVAILABLE = "qdrant_unavailable"
    LLM_TIMEOUT = "llm_timeout"
    LLM_RATE_LIMITING = "llm_rate_limiting"
    REPOSITORY_FAILURE = "repository_failure"
    RANDOM_EXCEPTION = "random_exception"
    PARTIAL_WORKFLOW_FAILURE = "partial_workflow_failure"


# Fault types that delay rather than fail outright — everything else in
# FaultType either raises or fails a fraction of calls.
_LATENCY_FAULTS = {FaultType.SLOW_DATABASE, FaultType.SLOW_API, FaultType.SLOW_AGENT}


class FaultSpec(BaseModel):
    """What to inject. `probability` (0.0-1.0) governs whether a given
    call actually triggers the fault — 1.0 (default) means every call
    is affected, useful for deterministic CI scenarios per the brief's
    §17 "prefer deterministic scenarios over random failures"; a value
    below 1.0 is only meaningful for `RANDOM_EXCEPTION`/
    `PARTIAL_WORKFLOW_FAILURE`, which are explicitly probabilistic by
    nature."""
    fault_type: FaultType
    target: str  # the component/service name this fault targets, e.g. "ArtifactRepository"
    delay_ms: Optional[float] = None  # for the _LATENCY_FAULTS
    probability: float = 1.0
    exception_message: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class FaultEvent(BaseModel):
    """What actually happened when a `FaultSpec` was applied — brief's
    §6 "no manual inspection should be required" starts here: every
    field is something `fault_injector.py` recorded automatically, not
    something a human annotated afterward."""
    fault_type: FaultType
    target: str
    injected_at: str
    triggered: bool  # False if probability rolled below the fault's own probability
    duration_ms: Optional[float] = None  # for latency faults: how long the injected delay was
    error_message: Optional[str] = None  # for failure faults: the message the wrapped call raised


class RecoverySignal(BaseModel):
    """Brief's §6 Recovery Validation fields. Every field is
    `Optional`/defaulted — same "missing is not zero, missing is not
    measured" convention `benchmark_models.py` uses throughout, since a
    scenario that doesn't wire up rollback checking shouldn't report
    `rollback_success=False` (a false negative) when the honest answer
    is "not checked"."""
    retried: bool = False
    retry_count: int = 0
    recovered: bool = False
    recovery_latency_ms: Optional[float] = None
    rollback_success: Optional[bool] = None
    incident_generated: Optional[bool] = None
    alert_generated: Optional[bool] = None
    dlq_routed: Optional[bool] = None
    workflow_completed: Optional[bool] = None
    checkpoint_recovered: Optional[bool] = None


class ScenarioResult(BaseModel):
    """One independent scenario execution (brief's §5 diagram:
    inject → execute → collect metrics → validate recovery → report).
    `benchmark` is this milestone's §13 Benchmark Integration point —
    a `benchmark_models.WorkflowBenchmark`-shaped dict produced by
    literally calling `benchmark_runner`'s own timing primitives
    around the workflow execution (see `scenario_runner.py`), not a
    reimplementation of them."""
    scenario_name: str
    faults: List[FaultEvent] = Field(default_factory=list)
    recovery: RecoverySignal = Field(default_factory=RecoverySignal)
    success: bool
    started_at: str
    ended_at: str
    duration_ms: float
    benchmark: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None


class ResilienceMetrics(BaseModel):
    """Brief's §7 metrics, computed by `resilience_analyzer.py` from a
    list of `ScenarioResult`s — every field here is a **derived**
    value (see `benchmark_runner.py`'s measured-vs-derived distinction,
    which applies identically here), deterministic given the inputs."""
    success_rate: float = 0.0
    failure_rate: float = 0.0
    mttr_ms: Optional[float] = None  # mean time to recovery, across scenarios that recovered
    retry_count_total: int = 0
    recovery_percentage: float = 0.0
    component_availability: Dict[str, float] = Field(default_factory=dict)
    incident_frequency: float = 0.0
    alert_latency_ms: Optional[float] = None
    workflow_completion_rate: float = 0.0
    scenario_count: int = 0


class ChaosRun(BaseModel):
    """One chaos-testing execution — mirrors
    `benchmark_models.BenchmarkRun`'s identity fields plus brief's §14
    Versioning Integration additions (`workflow_version`,
    `benchmark_version`, on top of `platform_version`/`commit_hash`/
    `environment` that `BenchmarkRun` already has). All version fields
    are caller-supplied pass-throughs from the real M4.1 version
    registry — this module does not compute or validate them itself."""
    name: str = "default"
    version: str = "1"
    timestamp: str
    platform_version: Optional[str] = None
    benchmark_version: Optional[str] = None
    workflow_version: Optional[str] = None
    commit_hash: Optional[str] = None
    environment: str = "unknown"

    scenarios: List[ScenarioResult] = Field(default_factory=list)
    metrics: ResilienceMetrics = Field(default_factory=ResilienceMetrics)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChaosRecord(BaseModel):
    """A `ChaosRun` as stored in the registry/DB, plus the assigned
    record id — same record/value split `benchmark_models.
    BenchmarkRecord` uses over `BenchmarkRun`."""
    id: Optional[str] = None
    run: ChaosRun


class MetricDelta(BaseModel):
    """One metric's before/after values — brief §15 "Regression
    detection," same atomic unit `benchmark_models.MetricDelta`
    established for M4.4, applied here to `ResilienceMetrics` fields."""
    metric: str
    baseline_value: Optional[float] = None
    current_value: Optional[float] = None
    percent_change: Optional[float] = None
    regressed: bool = False


class ChaosComparison(BaseModel):
    """Current-vs-baseline resilience comparison — produced by
    `resilience_analyzer.compare_runs`, consumed by `chaos_report.py`."""
    baseline_label: str
    current_label: str
    regression_threshold_pct: float = 10.0
    deltas: List[MetricDelta] = Field(default_factory=list)
    regressed_count: int = 0
    improved_count: int = 0
    unchanged_count: int = 0


class ChaosReport(BaseModel):
    """The rendered report `chaos_report.py` builds from one
    `ChaosRun` — brief's §11 required contents (injected faults,
    timeline, recovery actions, component health, resilience score,
    recommendations) all derive from `run` plus the two computed
    fields below; `chaos_report.py`'s `render_markdown`/`render_json`/
    `render_html` all consume this one shape, same "compute once,
    render N ways" split `benchmark_report.py` established."""
    run: ChaosRun
    resilience_score: float  # 0-100, derived from run.metrics — see chaos_report.compute_resilience_score
    recommendations: List[str] = Field(default_factory=list)
    comparison: Optional[ChaosComparison] = None
    generated_at: str
    summary: Dict[str, Any] = Field(default_factory=dict)


class ScenarioCatalogEntry(BaseModel):
    """One entry in the static scenario catalog `GET /platform/chaos/
    scenarios` serves — brief's §10 route. Distinct from
    `ScenarioResult` (that's what running one produces); this is what
    a caller sees before running anything."""
    name: str
    description: str
    fault_types: List[FaultType]
    requires_external_infra: List[str] = Field(default_factory=list)

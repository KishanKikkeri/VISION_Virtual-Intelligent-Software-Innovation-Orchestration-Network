"""
services/monitoring/leads — L4 leads: MetricsLead, ObservabilityLead, AlertingLead.

Each team lead runs its workers in dependency-aware batches
(services.monitoring.context.topological_batches / the _METRICS_DEPS /
_OBSERVABILITY_DEPS / _ALERTING_DEPS maps) — mirrors
services/devops/leads exactly (asyncio.gather within a batch,
sequential across batches).
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

import structlog

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.monitoring.context import (
    ALERTING_WORKERS,
    METRICS_WORKERS,
    OBSERVABILITY_WORKERS,
    _ALERTING_DEPS,
    _METRICS_DEPS,
    _OBSERVABILITY_DEPS,
    aggregate_component_scores,
)
from services.monitoring.models import MetricSample

log = structlog.get_logger(__name__)


def _artifact_type_of(a) -> Optional[str]:
    if isinstance(a, dict):
        return a.get("artifact_type")
    return getattr(a, "artifact_type", None)


def _ctx_record_result(task: TaskInput, worker_id: str, result: AgentResult) -> None:
    """Mirrors services.devops.leads._ctx_record_result — stores each
    worker's result under its worker_id so the lead/head can read it back."""
    if result.content and isinstance(result.content, dict):
        task.context.approved_artifacts[worker_id] = result.content
        for a in result.artifacts:
            t = _artifact_type_of(a)
            if t:
                task.context.approved_artifacts.setdefault(t, result.content)


async def _run_batches(task: TaskInput, factory, worker_ids: List[str], deps: Dict[str, List[str]]) -> List[AgentResult]:
    remaining = list(worker_ids)
    done: set = set()
    results: List[AgentResult] = []

    while remaining:
        batch = [w for w in remaining if all(d in done for d in deps.get(w, []))]
        if not batch:
            raise ValueError(f"Dependency cycle among Monitoring workers: {remaining}")

        async def _run_one(wid: str) -> AgentResult:
            if not factory:
                return AgentResult(task_id=task.task_id, agent_id=wid, status=TaskStatus.COMPLETED,
                                    content={"placeholder": True}, summary=f"{wid} placeholder", quality_score=0.8)
            r = await factory.create(wid).run(task)
            _ctx_record_result(task, wid, r)
            return r

        batch_results = await asyncio.gather(*[_run_one(w) for w in batch])
        results.extend(batch_results)
        for w in batch:
            done.add(w)
            remaining.remove(w)

    return results


def _aggregate(results: List[AgentResult]):
    all_arts, failures, min_score = [], [], 1.0
    for r in results:
        all_arts.extend(r.artifacts)
        min_score = min(min_score, r.quality_score)
        if r.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
            failures.append(f"{r.agent_id}: {r.failure_reason}")
    return all_arts, failures, min_score


@AgentFactory.register("metrics_lead")
class MetricsLead(BaseAgent):
    """
    Coordinates Infrastructure Metrics Worker + Application Metrics
    Worker, then aggregates their samples into per-component scores
    (spec §3 step "Aggregate") stored under `__component_scores__` for
    every downstream team/head to read.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, METRICS_WORKERS, _METRICS_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        samples: List[MetricSample] = []
        for r in results:
            if isinstance(r.content, dict):
                for raw in r.content.get("samples", []):
                    try:
                        samples.append(MetricSample(**raw))
                    except Exception:
                        continue

        component_scores = aggregate_component_scores(samples)
        task.context.approved_artifacts["__component_scores__"] = {
            c.value: v for c, v in component_scores.items()
        }
        task.context.approved_artifacts["__metric_samples__"] = [s.model_dump(mode="json") for s in samples]

        status = TaskStatus.FAILED if failures else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"team": "metrics", "samples_collected": len(samples), "failures": failures,
                     "component_scores": {c.value: v for c, v in component_scores.items()}},
            summary=f"Metrics collection: {len(samples)} sample(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
            failure_reason="; ".join(failures) if failures else None,
        )


@AgentFactory.register("observability_lead")
class ObservabilityLead(BaseAgent):
    """Coordinates Log Analysis Worker + Trace Analysis Worker into a `performance_report`."""

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, OBSERVABILITY_WORKERS, _OBSERVABILITY_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        error_rate, p95_latency_ms, hotspots = 0.0, 0.0, []
        for r in results:
            if not isinstance(r.content, dict):
                continue
            if "error_rate" in r.content:
                error_rate = r.content["error_rate"]
            if "p95_latency_ms" in r.content:
                p95_latency_ms = r.content["p95_latency_ms"]
            if "trace_hotspots" in r.content:
                hotspots = r.content["trace_hotspots"]

        performance = {"p95_latency_ms": p95_latency_ms, "error_rate": error_rate, "trace_hotspots": hotspots}
        task.context.approved_artifacts["__performance_report__"] = performance

        artifact = await self.create_artifact(task, "performance_report", performance)

        status = TaskStatus.FAILED if failures else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"team": "observability", **performance, "failures": failures},
            summary=f"Observability: error_rate={error_rate}, p95={p95_latency_ms}ms",
            quality_score=min_score, artifacts=all_arts + [artifact],
            failure_reason="; ".join(failures) if failures else None,
        )


@AgentFactory.register("alerting_lead")
class AlertingLead(BaseAgent):
    """
    Coordinates Alert Worker + Dashboard Worker — the only team allowed
    to publish monitoring.alert/.warning/.incident and to write
    alerts/alert_history (spec §2 separation-of-duties rule).
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        results = await _run_batches(task, factory, ALERTING_WORKERS, _ALERTING_DEPS)
        all_arts, failures, min_score = _aggregate(results)

        status = TaskStatus.FAILED if failures else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content={"team": "alerting", "executed": len(results), "failures": failures},
            summary=f"Alerting: {len(results)} step(s), {len(failures)} failure(s)",
            quality_score=min_score, artifacts=all_arts,
            failure_reason="; ".join(failures) if failures else None,
        )

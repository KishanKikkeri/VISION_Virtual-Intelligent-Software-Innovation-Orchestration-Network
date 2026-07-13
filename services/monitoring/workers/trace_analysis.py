"""services/monitoring/workers/trace_analysis.py — Trace Analysis Worker.

Reads the existing OTel-adjacent `aasc_agent_run_duration_seconds`
Histogram (infrastructure/monitoring/telemetry.py) to rank which
agent/department is the current latency hotspot — a lightweight,
in-process substitute for querying a full external trace backend,
consistent with spec §0 Decision 2 (reuse existing telemetry, don't
duplicate it).
"""
from __future__ import annotations

from typing import Dict, List

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory

TOP_N_HOTSPOTS = 5


@AgentFactory.register("trace_analysis_worker")
class TraceAnalysisWorker(BaseAgent):
    """Deterministic — no LLM call."""

    async def execute(self, task: TaskInput) -> AgentResult:
        try:
            from infrastructure.monitoring.telemetry import agent_run_duration

            families = agent_run_duration.collect()
            samples = list(families[0].samples) if families else []

            sums: Dict[str, float] = {}
            counts: Dict[str, float] = {}
            for s in samples:
                agent_id = s.labels.get("agent_id", "unknown")
                if s.name.endswith("_sum"):
                    sums[agent_id] = sums.get(agent_id, 0.0) + s.value
                elif s.name.endswith("_count"):
                    counts[agent_id] = counts.get(agent_id, 0.0) + s.value

            avg_by_agent = {
                agent_id: (sums.get(agent_id, 0.0) / counts[agent_id])
                for agent_id in counts if counts[agent_id] > 0
            }
            hotspots: List[str] = [
                agent_id for agent_id, _ in
                sorted(avg_by_agent.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N_HOTSPOTS]
            ]
            p95_latency_ms = round(max(avg_by_agent.values()) * 1000, 2) if avg_by_agent else 0.0

            return AgentResult(
                task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
                content={"trace_hotspots": hotspots, "p95_latency_ms": p95_latency_ms},
                summary=f"{len(hotspots)} latency hotspot(s) identified" if hotspots
                        else "No agent runtime activity recorded this process lifetime",
                quality_score=1.0,
            )
        except Exception as e:
            return AgentResult(
                task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
                content={"trace_hotspots": [], "p95_latency_ms": 0.0},
                summary=f"trace analysis degraded: {e}",
                quality_score=0.5,
            )

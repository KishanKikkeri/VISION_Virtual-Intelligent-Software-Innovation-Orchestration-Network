"""
services/integration/chaos/chaos_repository.py
=================================
M4.5 §8 "Repository Pattern" — "never access SQLAlchemy directly from
APIs / scenario runner / fault injector / report generator." Every
other module in this package only ever imports `ChaosRepository`
(never `infrastructure.database.chaos_testing_models` directly), same
convention M4.4's `benchmark_repository.py` set for its own callers.

`record_run` flattens a `chaos_models.ChaosRun` into: a find-or-create
`ChaosScenario` catalog row, one `ChaosRun` row (identity fields +
JSON blobs for exact round-trip), one `FaultEvent` row per fault
actually injected across every scenario in the run, and one
`ResilienceReport` row (the persisted score/recommendations —
computed by `resilience_analyzer.py`/`chaos_report.py` and handed in,
not recomputed here).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from services.integration.chaos.chaos_models import ChaosRecord
from services.integration.chaos.chaos_models import ChaosRun as ChaosRunModel


class ChaosRepository:

    @staticmethod
    async def ensure_scenario(
        db: Any, name: str, description: Optional[str] = None,
        fault_types: Optional[List[str]] = None, requires_external_infra: Optional[List[str]] = None,
    ) -> Any:
        """Idempotent find-or-create — same idiom
        `BenchmarkRepository.ensure_benchmark` uses for its own named
        suites."""
        from infrastructure.database.chaos_testing_models import ChaosScenario

        result = await db.execute(select(ChaosScenario).where(ChaosScenario.name == name))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing

        row = ChaosScenario(
            name=name, description=description,
            fault_types=fault_types or [], requires_external_infra=requires_external_infra or [],
        )
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def record_run(db: Any, run: ChaosRunModel, resilience_score: float,
                          recommendations: List[str], summary: Optional[Dict[str, Any]] = None) -> str:
        """Persists one full `ChaosRun`: find-or-create the scenario
        catalog row, one `ChaosRun` row, one `FaultEvent` row per fault
        across every one of the run's scenario results, and one
        `ResilienceReport` row. `resilience_score`/`recommendations`
        are supplied by the caller (`chaos_report.build_report` already
        computed them via `resilience_analyzer.py`) rather than
        recomputed here — this repository persists, it does not
        analyze."""
        from infrastructure.database.chaos_testing_models import ChaosRun as ChaosRunRow
        from infrastructure.database.chaos_testing_models import FaultEvent as FaultEventRow
        from infrastructure.database.chaos_testing_models import ResilienceReport

        scenario_row = await ChaosRepository.ensure_scenario(db, run.name)

        run_row = ChaosRunRow(
            scenario_id=scenario_row.id, version=run.version,
            timestamp=datetime.fromisoformat(run.timestamp) if run.timestamp else datetime.utcnow(),
            workflow_version=run.workflow_version, platform_version=run.platform_version,
            benchmark_version=run.benchmark_version, commit_hash=run.commit_hash, environment=run.environment,
            metrics=run.metrics.model_dump(mode="json"),
            scenario_results=[s.model_dump(mode="json") for s in run.scenarios],
            metadata_=run.metadata,
        )
        db.add(run_row)
        await db.flush()

        for scenario_result in run.scenarios:
            for fault in scenario_result.faults:
                db.add(FaultEventRow(
                    chaos_run_id=run_row.id, scenario_name=scenario_result.scenario_name,
                    fault_type=fault.fault_type.value, target=fault.target,
                    injected_at=datetime.fromisoformat(fault.injected_at) if fault.injected_at else None,
                    triggered=fault.triggered, duration_ms=fault.duration_ms, error_message=fault.error_message,
                ))

        db.add(ResilienceReport(
            chaos_run_id=run_row.id, resilience_score=resilience_score,
            recommendations=recommendations, summary=summary or {},
        ))
        await db.flush()
        return run_row.id

    @staticmethod
    async def get_latest(db: Any, name: str) -> Optional[ChaosRecord]:
        from infrastructure.database.chaos_testing_models import ChaosScenario
        from infrastructure.database.chaos_testing_models import ChaosRun as ChaosRunRow

        result = await db.execute(
            select(ChaosRunRow)
            .join(ChaosScenario, ChaosScenario.id == ChaosRunRow.scenario_id)
            .where(ChaosScenario.name == name)
            .options(selectinload(ChaosRunRow.resilience_report))
            .order_by(ChaosRunRow.timestamp.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return _row_to_record(name, row) if row is not None else None

    @staticmethod
    async def get_by_id(db: Any, run_id: str) -> Optional[ChaosRecord]:
        from infrastructure.database.chaos_testing_models import ChaosScenario
        from infrastructure.database.chaos_testing_models import ChaosRun as ChaosRunRow

        result = await db.execute(
            select(ChaosRunRow, ChaosScenario.name)
            .join(ChaosScenario, ChaosScenario.id == ChaosRunRow.scenario_id)
            .where(ChaosRunRow.id == run_id)
            .options(selectinload(ChaosRunRow.resilience_report))
        )
        row_and_name = result.first()
        if row_and_name is None:
            return None
        row, name = row_and_name
        return _row_to_record(name, row)

    @staticmethod
    async def list_history(db: Any, name: str, limit: int = 50) -> List[ChaosRecord]:
        from infrastructure.database.chaos_testing_models import ChaosScenario
        from infrastructure.database.chaos_testing_models import ChaosRun as ChaosRunRow

        result = await db.execute(
            select(ChaosRunRow)
            .join(ChaosScenario, ChaosScenario.id == ChaosRunRow.scenario_id)
            .where(ChaosScenario.name == name)
            .options(selectinload(ChaosRunRow.resilience_report))
            .order_by(ChaosRunRow.timestamp.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())
        rows.reverse()
        return [_row_to_record(name, row) for row in rows]

    @staticmethod
    async def list_scenarios(db: Any) -> List[Dict[str, Any]]:
        from infrastructure.database.chaos_testing_models import ChaosScenario

        result = await db.execute(select(ChaosScenario).order_by(ChaosScenario.name))
        return [{
            "name": s.name, "description": s.description,
            "fault_types": s.fault_types or [], "requires_external_infra": s.requires_external_infra or [],
            "created_at": s.created_at.isoformat() if s.created_at else None,
        } for s in result.scalars().all()]

    @staticmethod
    async def list_fault_events(db: Any, run_id: str) -> List[Dict[str, Any]]:
        """Backs any caller wanting the queryable fault timeline
        directly (rather than via the JSON blob) — e.g. "every fault
        against `ArtifactRepository` across this run."""
        from infrastructure.database.chaos_testing_models import FaultEvent as FaultEventRow

        result = await db.execute(
            select(FaultEventRow).where(FaultEventRow.chaos_run_id == run_id).order_by(FaultEventRow.injected_at)
        )
        return [{
            "scenario_name": f.scenario_name, "fault_type": f.fault_type, "target": f.target,
            "injected_at": f.injected_at.isoformat() if f.injected_at else None,
            "triggered": f.triggered, "duration_ms": f.duration_ms, "error_message": f.error_message,
        } for f in result.scalars().all()]


def _row_to_record(name: str, row: Any) -> ChaosRecord:
    """Reassembles a `chaos_models.ChaosRun` from a `ChaosRunRow` ORM
    row — the inverse of `record_run`'s flattening. Reads `metrics`/
    `scenario_results` back from their JSON blobs (see
    `chaos_testing_models.ChaosRun`'s docstring for why those blobs
    exist alongside the `FaultEvent` rows)."""
    from services.integration.chaos.chaos_models import ResilienceMetrics, ScenarioResult

    metrics = ResilienceMetrics(**(row.metrics or {}))
    scenarios = [ScenarioResult(**s) for s in (row.scenario_results or [])]

    run = ChaosRunModel(
        name=name, version=row.version,
        timestamp=row.timestamp.isoformat() if row.timestamp else "",
        workflow_version=row.workflow_version, platform_version=row.platform_version,
        benchmark_version=row.benchmark_version, commit_hash=row.commit_hash, environment=row.environment,
        scenarios=scenarios, metrics=metrics, metadata=row.metadata_ or {},
    )
    return ChaosRecord(id=row.id, run=run)

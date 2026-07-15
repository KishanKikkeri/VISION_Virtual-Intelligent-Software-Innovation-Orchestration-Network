"""
services/integration/repository.py
=================================
Repository-pattern wrapper around the M3.9 ORM tables (PlatformReport,
ValidationResult, DependencyCheck — infrastructure/database/models.py).
Mirrors services/incident_response/integration/incident_repository.py's
self-contained precedent.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select

from infrastructure.database.models import DependencyCheck, PlatformReport, ValidationResult


class PlatformReportRepository:
    @staticmethod
    async def record(db, readiness_overall: float, readiness_categories: Dict[str, Any],
                      health_overall: str, summary: str = "") -> PlatformReport:
        row = PlatformReport(readiness_overall=readiness_overall, readiness_categories=readiness_categories,
                              health_overall=health_overall, summary=summary)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def latest(db) -> Optional[PlatformReport]:
        r = await db.execute(select(PlatformReport).order_by(PlatformReport.generated_at.desc()).limit(1))
        return r.scalar_one_or_none()

    @staticmethod
    async def list_recent(db, limit: int = 20) -> List[PlatformReport]:
        r = await db.execute(select(PlatformReport).order_by(PlatformReport.generated_at.desc()).limit(limit))
        return list(r.scalars().all())


class ValidationResultRepository:
    @staticmethod
    async def record(db, report_id: str, category: str, passed: bool,
                      detail: Dict[str, Any]) -> ValidationResult:
        row = ValidationResult(report_id=report_id, category=category, passed=passed, detail=detail)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def list_for_report(db, report_id: str) -> List[ValidationResult]:
        r = await db.execute(select(ValidationResult).where(ValidationResult.report_id == report_id))
        return list(r.scalars().all())


class DependencyCheckRepository:
    @staticmethod
    async def record(db, report_id: str, department: str, passed: bool,
                      missing: Dict[str, Any]) -> DependencyCheck:
        row = DependencyCheck(report_id=report_id, department=department, passed=passed, missing=missing)
        db.add(row)
        await db.flush()
        return row

    @staticmethod
    async def list_for_report(db, report_id: str) -> List[DependencyCheck]:
        r = await db.execute(select(DependencyCheck).where(DependencyCheck.report_id == report_id))
        return list(r.scalars().all())

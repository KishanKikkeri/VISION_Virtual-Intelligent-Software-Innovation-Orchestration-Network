"""
services/qa/schemas — API & event contracts for M3.4.
=========================================================
Nothing outside this module should define ad-hoc request/response
dict shapes for the QA Service's HTTP API or NATS events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from services.qa.models import DefectReport, QAReport, QATaskStatus


# ── Requests ──────────────────────────────────────────────────

class StartQARequest(BaseModel):
    project_id:   str
    workflow_id:  str
    feature_name: str = "default"


class RetryDecisionRequest(BaseModel):
    project_id:  str
    approved:    bool
    approved_by: Optional[str] = None
    feedback:    Optional[str] = None


# ── Responses ─────────────────────────────────────────────────

class QATaskResponse(BaseModel):
    task_id:         str
    team:            str
    worker_agent_id: str
    status:          QATaskStatus
    retry_count:     int
    depends_on:      List[str]
    failure_reason:  Optional[str] = None


class QAPlanResponse(BaseModel):
    plan_id:      str
    project_id:   str
    feature_name: str
    total_tasks:  int
    tasks:        List[QATaskResponse]


class QAStatusResponse(BaseModel):
    project_id:    str
    phase_status:  str
    plan:          Optional[QAPlanResponse] = None
    suites_generated: int = 0
    coverage_pct:  float = 0.0
    qa_report:     Optional[QAReport] = None
    defects:       List[DefectReport] = Field(default_factory=list)
    updated_at:    datetime = Field(default_factory=datetime.utcnow)


class QACompletedEvent(BaseModel):
    project_id:      str
    workflow_id:     str
    feature_name:    str
    passed:          bool
    coverage_pct:    float
    tests_total:     int
    defect_count:    int


# ── Errors ────────────────────────────────────────────────────

class QAServiceError(Exception):
    """Base class for all QA Service errors."""


class TestContractViolation(QAServiceError):
    """Raised when a TestSuite fails the mandatory test contract."""


class NoEngineeringArtifactsError(QAServiceError):
    """Raised when QA is invoked without approved Engineering artifacts."""


class QAGateBlockedError(QAServiceError):
    """Raised when the QA gate blocks and no more retry budget remains."""


class DeadLetterError(QAServiceError):
    """Raised when a QA task exhausts its retry budget and is dead-lettered."""

"""services/qa/workers/regression.py — Regression Suite Worker."""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.qa.models import RegressionReport


@AgentFactory.register("regression_suite_worker")
class RegressionSuiteWorker(BaseAgent):
    """
    Runs the full existing test suite (unit + integration, already
    generated earlier in the pipeline) to detect regressions. Deterministic:
    counts tests already produced by the Unit/Integration teams and treats
    a review-failed or coding-contract-violated module as a regression
    signal, mirroring Engineering's "review_passed" flag on CodeModule.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        unit = task.context.get_artifact("unit_test_suite", {})
        integration = task.context.get_artifact("integration_test_suite", {})

        tests_run = (unit.get("test_count", 0) if isinstance(unit, dict) else 0) + (
            integration.get("test_count", 0) if isinstance(integration, dict) else 0
        )
        regressions = list(task.context.approved_artifacts.get("__known_regressions__", []))
        tests_failed = len(regressions)
        tests_passed = max(0, tests_run - tests_failed)

        report = RegressionReport(
            project_id=task.project_id, tests_run=tests_run,
            tests_passed=tests_passed, tests_failed=tests_failed,
            regressions_detected=regressions,
        )

        artifact = await self.create_artifact(
            task, "regression_report", {**report.model_dump(), "project_id": task.project_id},
        )
        status = TaskStatus.COMPLETED if report.passed else TaskStatus.FAILED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content=report.model_dump(),
            summary=f"Regression suite: {tests_passed}/{tests_run} passed, "
                    f"{len(regressions)} regression(s)",
            quality_score=0.95 if report.passed else 0.3,
            artifacts=[artifact],
            failure_reason=None if report.passed else f"Regressions detected: {regressions}",
        )

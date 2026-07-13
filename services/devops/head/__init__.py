"""
services/devops/head — L3 DevOpsHead: orchestrates the full M3.6 pipeline.

Two-stage pipeline, split around Manager's deployment approval interrupt
(services/manager/graphs/lifecycle.py's deployment_approval_gate_node):

  STAGE A — generate_deployment_plan (task_type != "execute_deployment")
    Receive Manager Approval trigger (QA+Security both completed)
    -> Validate QA + Security
    -> Generate Infrastructure (container_lead)
    -> Generate CI/CD (cicd_lead)
    -> produce `deployment_plan` artifact, STOP and let Manager's
       approval interrupt take over (no deployment executes yet)

  STAGE B — execute_deployment (task_type == "execute_deployment",
            triggered by Manager after a human approves the plan)
    -> Deploy (infrastructure_ops_lead: provisioner_worker)
    -> Health Check (infrastructure_ops_lead: health_check_worker)
    -> PASS -> Release (version/tag/notes) -> devops.phase.completed
    -> FAIL -> Rollback -> devops.phase.completed (status=rolled_back/failed)

See docs/M3.6_DevOps_Service_Handover.md for the full design rationale.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import structlog

from core.contracts import AgentResult, NATSEvent, TaskStatus, WebSocketEvent
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.devops.context import (
    build_deployment_plan,
    build_deployment_report,
    build_devops_plan,
    build_health_report,
    build_release,
    build_rollback_report,
    decide_rollback,
    validate_qa_and_security,
)
from services.devops.integration.deployment_repository import (
    DeploymentHistoryRepository,
    DeploymentRepository,
    ReleaseMetadataRepository,
    RollbackRecordRepository,
)
from services.devops.integration.repository_client import DevOpsRepositoryClient
from services.devops.models import DeploymentStatus, VersionBump
from services.devops.providers import default_provider
from services.devops.utils import bump_version

log = structlog.get_logger(__name__)

INFRA_CICD_LEADS = [("container_lead", "Generate Infrastructure"), ("cicd_lead", "Generate CI/CD")]

# Only source_code + qa_report + security_report are hard-required to
# even generate a deployment plan. deployment_plan itself is the output
# of Stage A, not an input to it. Deployment Plan / Docker Configuration
# / Environment Configuration / Version Information become *inputs*
# only for Stage B (execute_deployment), once Stage A has produced them.
STAGE_A_REQUIRED_ARTIFACTS = ("source_code", "qa_report", "security_report")
STAGE_B_REQUIRED_ARTIFACTS = ("deployment_plan",)


def _artifact_type_of(a) -> Optional[str]:
    if isinstance(a, dict):
        return a.get("artifact_type")
    return getattr(a, "artifact_type", None)


def _artifact_id_of(a) -> Optional[str]:
    if isinstance(a, dict):
        return a.get("artifact_id")
    return getattr(a, "artifact_id", None)


def _refs_by_type(artifacts) -> Dict[str, str]:
    refs: Dict[str, str] = {}
    for a in artifacts:
        t, i = _artifact_type_of(a), _artifact_id_of(a)
        if t and i:
            refs.setdefault(t, i)
    return refs


@AgentFactory.register("devops_head")
class DevOpsHead(BaseAgent):
    """L3 — Sole orchestrator of devops-service."""

    async def execute(self, task: TaskInput) -> AgentResult:
        if task.task_type == "execute_deployment":
            return await self._execute_deployment_stage(task)
        return await self._generate_plan_stage(task)

    # -- Stage A --------------------------------------------------

    async def _generate_plan_stage(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        feature_name = task.context.approved_artifacts.get("__feature_name__", "default")

        await self.notify_ui(task.project_id, "phase_started", {
            "phase": 8, "phase_name": "DevOps — Infrastructure & CI/CD",
            "message": "Generating deployment plan for review",
        })
        await self.publish_event("devops.phase.started",
            {"project_id": task.project_id, "feature_name": feature_name, "stage": "generate_plan"})

        missing = [a for a in STAGE_A_REQUIRED_ARTIFACTS if not task.context.get_artifact(a)]
        if missing:
            reason = f"Missing required upstream artifact(s): {missing}"
            await self.publish_event("devops.phase.failed", {"project_id": task.project_id, "reason": reason})
            return self.escalate(task, reason)

        qa_report = task.context.get_artifact("qa_report", {})
        security_report = task.context.get_artifact("security_report", {})
        blocking = validate_qa_and_security(qa_report, security_report)
        if blocking:
            reason = "; ".join(blocking)
            await self.publish_event("devops.phase.failed", {"project_id": task.project_id, "reason": reason})
            return self.escalate(task, f"DevOps blocked: {reason}")

        plan_skeleton = build_devops_plan(task.project_id, feature_name, task.context.approved_artifacts)
        task.context.approved_artifacts["__devops_plan__"] = plan_skeleton.model_dump()

        # -- Generate Infrastructure -> Generate CI/CD (sequential, per spec) --
        all_artifacts = []
        for agent_id, step in INFRA_CICD_LEADS:
            if not factory:
                continue
            r = await factory.create(agent_id).run(task)
            await self.notify_ui(task.project_id, "agent_completed",
                {"agent": agent_id, "step": step, "status": r.status.value, "score": r.quality_score})
            all_artifacts.extend(r.artifacts)
            if r.status == TaskStatus.FAILED:
                reason = f"{step} failed: {r.failure_reason}"
                await self.publish_event("devops.phase.failed", {"project_id": task.project_id, "reason": reason})
                return self.escalate(task, reason)

        artifact_refs = _refs_by_type(all_artifacts)

        previous_version = await self._safe_get_latest_release_version(task.project_id)
        proposed_version = bump_version(previous_version, VersionBump.MINOR)

        plan = build_deployment_plan(
            project_id=task.project_id, qa_report=qa_report, security_report=security_report,
            proposed_version=proposed_version, artifact_refs=artifact_refs, blocking_reasons=[],
        )
        plan_artifact = await self.create_artifact(task, "deployment_plan", plan.model_dump())
        all_artifacts.append(plan_artifact)

        deployment_id = await self._safe_create_deployment_row(
            task, version=proposed_version, deployment_plan_ref=_artifact_id_of(plan_artifact))

        await self.write_memory(
            task, f"DevOps generated deployment plan {plan.plan_id} (v{proposed_version}) "
                  f"for {task.project_id}, awaiting approval", source="devops_head",
        )

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"phase": "devops", "stage": "plan_generated",
                     "deployment_plan": plan.model_dump(), "deployment_id": deployment_id},
            summary=f"Deployment plan ready for review (v{proposed_version}) — awaiting approval",
            quality_score=1.0, artifacts=all_artifacts,
            nats_events=[NATSEvent(subject="devops.plan.ready",
                                    payload={"project_id": task.project_id, "version": proposed_version},
                                    project_id=task.project_id)],
            ws_events=[WebSocketEvent(project_id=task.project_id, event_type="approval_required",
                payload={"artifact_type": "deployment_plan",
                         "message": "Deployment plan ready — please review before we deploy"})],
        )

    # -- Stage B --------------------------------------------------

    async def _execute_deployment_stage(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        provider = task.context.approved_artifacts.get("__provider__") or default_provider()

        await self.notify_ui(task.project_id, "phase_started", {
            "phase": 8, "phase_name": "DevOps — Deployment", "message": "Deploying approved plan",
        })
        await self.publish_event("devops.phase.started",
            {"project_id": task.project_id, "stage": "execute_deployment"})

        missing = [a for a in STAGE_B_REQUIRED_ARTIFACTS if not task.context.get_artifact(a)]
        if missing:
            reason = f"Missing required artifact(s): {missing}"
            await self.publish_event("devops.phase.failed", {"project_id": task.project_id, "reason": reason})
            return self.escalate(task, reason)

        plan = task.context.get_artifact("deployment_plan", {})
        qa_report = task.context.get_artifact("qa_report", {})
        security_report = task.context.get_artifact("security_report", {})

        deployment_id = await self._safe_get_or_create_deployment_row(task, plan)
        task.context.approved_artifacts["__deployment_id__"] = deployment_id
        task.context.approved_artifacts["__provider__"] = provider

        await self.publish_event("deployment.started",
            {"project_id": task.project_id, "deployment_id": deployment_id, "version": plan.get("proposed_version")})

        all_artifacts = []
        if factory:
            r = await factory.create("infrastructure_ops_lead").run(task)
            all_artifacts.extend(r.artifacts)
            # infrastructure_ops_lead's own workers already wrote their
            # results into task.context.approved_artifacts (see
            # services.devops.leads._ctx_record_result), the same
            # convention QA/Security leads use.

        provisioner_result = task.context.approved_artifacts.get("provisioner_worker", {})
        health_result = task.context.approved_artifacts.get("health_check_worker", {})
        deploy_succeeded = bool(provisioner_result.get("success"))
        deployment_ref = provisioner_result.get("deployment_ref", "")

        health_report = build_health_report(task.project_id, deployment_id or "unknown",
                                             health_result.get("checks", []))

        rollback_reason = decide_rollback(health_report, deploy_succeeded)

        if rollback_reason:
            return await self._rollback_and_report(
                task, deployment_id, deployment_ref, provider, rollback_reason,
                health_report, all_artifacts)

        return await self._release_and_report(
            task, deployment_id, plan, qa_report, security_report, health_report, all_artifacts)

    # -- Rollback path ----------------------------------------------

    async def _rollback_and_report(self, task, deployment_id, deployment_ref, provider,
                                    reason, health_report, all_artifacts) -> AgentResult:
        rb_result = await provider.rollback(task.project_id, deployment_ref, reason)
        succeeded = bool(rb_result.get("success"))

        previous_version = await self._safe_get_latest_release_version(task.project_id)
        rollback_report = build_rollback_report(
            task.project_id, deployment_id or "unknown", reason, previous_version, succeeded)
        rb_artifact = await self.create_artifact(task, "rollback_report", rollback_report.model_dump())
        all_artifacts.append(rb_artifact)

        final_status = DeploymentStatus.ROLLED_BACK if succeeded else DeploymentStatus.FAILED
        if deployment_id:
            try:
                async with self._db_factory() as db:
                    await DeploymentRepository.update_status(db, deployment_id, final_status.value, failure_reason=reason)
                    await RollbackRecordRepository.create(
                        db, deployment_id, task.project_id, reason, previous_version,
                        status="completed" if succeeded else "failed")
                    await DeploymentHistoryRepository.record(
                        db, deployment_id, task.project_id, "rollback.completed", final_status.value,
                        payload={"reason": reason, "succeeded": succeeded})
            except Exception:
                pass

        report = build_deployment_report(
            task.project_id, deployment_id or "unknown", final_status, previous_version,
            health_report, rollback_report, None, [reason])
        report_artifact = await self.create_artifact(task, "deployment_report", report.model_dump())
        all_artifacts.append(report_artifact)

        await self.publish_event("rollback.completed",
            {"project_id": task.project_id, "deployment_id": deployment_id, "reason": reason, "succeeded": succeeded})
        await self.publish_event("deployment.failed",
            {"project_id": task.project_id, "deployment_id": deployment_id, "reason": reason})
        completed_payload = {"project_id": task.project_id, "passed": False, "status": final_status.value}
        await self.publish_event("devops.phase.completed", completed_payload)

        await self.write_memory(
            task, f"DevOps rolled back deployment for {task.project_id}: {reason}", source="devops_head")

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.FAILED,
            content={"phase": "devops", "stage": "rolled_back", "status": final_status.value,
                     "deployment_report": report.model_dump(), "rollback_report": rollback_report.model_dump()},
            summary=f"Deployment rolled back: {reason}",
            quality_score=0.0, artifacts=all_artifacts,
            nats_events=[NATSEvent(subject="devops.phase.completed", payload=completed_payload, project_id=task.project_id)],
            ws_events=[WebSocketEvent(project_id=task.project_id, event_type="phase_failed",
                payload={"phase": 8, "reason": reason, "message": "Deployment rolled back"})],
            failure_reason=reason,
        )

    # -- Success / release path ---------------------------------------

    async def _release_and_report(self, task, deployment_id, plan, qa_report, security_report,
                                   health_report, all_artifacts) -> AgentResult:
        version = plan.get("proposed_version", "0.1.0")
        previous_version = await self._safe_get_latest_release_version(task.project_id)

        release = build_release(task.project_id, version, previous_version, qa_report, security_report)

        try:
            repo_client = DevOpsRepositoryClient()
            await repo_client.create_release(task.project_id, tag_name=f"v{version}",
                                               name=f"Release {version}", body=release.release_notes)
        except Exception as e:
            log.warning("devops_release_tag_failed", error=str(e))

        if deployment_id:
            try:
                async with self._db_factory() as db:
                    await DeploymentRepository.update_status(db, deployment_id, "healthy")
                    await ReleaseMetadataRepository.create(
                        db, task.project_id, version, deployment_id=deployment_id,
                        previous_version=previous_version, release_notes=release.release_notes)
                    await DeploymentHistoryRepository.record(
                        db, deployment_id, task.project_id, "deployment.completed", "healthy",
                        payload={"version": version})
            except Exception:
                pass

        report = build_deployment_report(
            task.project_id, deployment_id or "unknown", DeploymentStatus.HEALTHY, version,
            health_report, None, release, [])
        report_artifact = await self.create_artifact(task, "deployment_report", report.model_dump())
        all_artifacts.append(report_artifact)

        await self.publish_event("health.completed",
            {"project_id": task.project_id, "deployment_id": deployment_id, "all_passed": True})
        await self.publish_event("deployment.completed",
            {"project_id": task.project_id, "deployment_id": deployment_id, "version": version})
        completed_payload = {"project_id": task.project_id, "passed": True,
                              "status": "healthy", "version": version}
        await self.publish_event("devops.phase.completed", completed_payload)

        await self.write_memory(
            task, f"DevOps deployed {task.project_id} successfully (v{version})", source="devops_head")

        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"phase": "devops", "stage": "deployed", "status": "healthy", "version": version,
                     "deployment_report": report.model_dump()},
            summary=f"Deployment succeeded (v{version}) — all health checks passed",
            quality_score=1.0, artifacts=all_artifacts,
            nats_events=[NATSEvent(subject="devops.phase.completed", payload=completed_payload, project_id=task.project_id)],
            ws_events=[WebSocketEvent(project_id=task.project_id, event_type="phase_completed",
                payload={"phase": 8, "phase_name": "DevOps", "message": "Deployment successful"})],
        )

    # -- DB helpers (best-effort — never block the pipeline result) --

    async def _safe_get_latest_release_version(self, project_id: str) -> Optional[str]:
        try:
            async with self._db_factory() as db:
                rel = await ReleaseMetadataRepository.get_latest_for_project(db, project_id)
                return rel.version if rel else None
        except Exception:
            return None

    async def _safe_create_deployment_row(self, task: TaskInput, version: str,
                                           deployment_plan_ref: Optional[str]) -> Optional[str]:
        try:
            async with self._db_factory() as db:
                d = await DeploymentRepository.create(
                    db, task.project_id, workflow_id=task.context.workflow_id,
                    version=version, deployment_plan_ref=deployment_plan_ref)
                await DeploymentHistoryRepository.record(
                    db, d.id, task.project_id, "plan.generated", "awaiting_approval",
                    payload={"version": version})
                await DeploymentRepository.update_status(db, d.id, "awaiting_approval")
                return d.id
        except Exception:
            return None

    async def _safe_get_or_create_deployment_row(self, task: TaskInput, plan: Dict[str, Any]) -> Optional[str]:
        try:
            async with self._db_factory() as db:
                existing = await DeploymentRepository.get_latest_for_project(db, task.project_id)
                if existing:
                    return existing.id
                d = await DeploymentRepository.create(
                    db, task.project_id, workflow_id=task.context.workflow_id,
                    version=plan.get("proposed_version"))
                return d.id
        except Exception:
            return None

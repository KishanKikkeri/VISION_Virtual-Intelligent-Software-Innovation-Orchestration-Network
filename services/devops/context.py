"""
services/devops/context.py — task decomposition + deterministic
"Deployment Lead" (rollback decision) and "Release Lead" (version /
release notes / deployment report) logic.

Design decision (see docs/M3.6_DevOps_Service_Handover.md, "Department
Structure"): AGENT_REGISTRY reserves exactly 10 agent_ids for the
`devops` department — 1 head, 3 leads (container/cicd/
infrastructure_ops), 6 workers — not the spec's 4-lead/12-worker chart
(Infrastructure Lead, CI/CD Lead, Deployment Lead, Release Lead). Per
the M3.6 constraints ("Do NOT modify ... AgentFactory"; "adapt the
implementation, document deviations"), DevOps is implemented against
the already-registered agents:

  Spec responsibility                          Where it lives
  --------------------------------------------  --------------------------------
  Docker Worker                                 dockerfile_writer_worker (container_lead)
  Compose Worker                                docker_compose_worker (container_lead)
  Kubernetes Worker (V2 stub)                    providers/kubernetes.py — no agent_id
                                                  reserved; interface only, per spec
  Environment Worker                             environment_config_worker (cicd_lead) —
                                                  registry parents it under CI/CD, not
                                                  Infrastructure; kept as-is per "never
                                                  rename/move already-reserved agents"
  GitHub Actions Worker + Pipeline Worker        pipeline_config_worker (cicd_lead) — one
                                                  worker satisfies both spec responsibilities
  Artifact Publishing Worker                     Deterministic — DevOpsHead publishes every
                                                  produced artifact through the standard
                                                  create_artifact() path already used
                                                  platform-wide; no separate agent needed
  Deployment Worker                              provisioner_worker (infrastructure_ops_lead)
  Health Check Worker                            health_check_worker (infrastructure_ops_lead)
  Rollback Worker                                Deterministic — decide_rollback() in this
                                                  module, invoked by DevOpsHead
  Version / Release Notes / Deployment Report    Deterministic — bump_version() (utils.py),
    Worker ("Release Lead")                       build_release()/build_deployment_report()
                                                   in this module, invoked by DevOpsHead

This is the third time this exact pattern appears (QA's "Reporting
Lead" in M3.4, Security's "Risk Lead" in M3.5, now DevOps's "Deployment
Lead"/"Release Lead") — plain Python functions standing in for spec
responsibilities that don't have a reserved agent_id, rather than
inventing unregistered IDs.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from services.devops.models import (
    DeploymentPlan,
    DeploymentReport,
    DeploymentStatus,
    DevOpsPlan,
    DevOpsTask,
    HealthReport,
    Release,
    RollbackReport,
    RollbackStatus,
    VersionBump,
    WorkerTeam,
)
from services.devops.utils import bump_version

# Worker assignment per team, matching AGENT_REGISTRY's devops-service entries.
INFRASTRUCTURE_WORKERS = ["dockerfile_writer_worker", "docker_compose_worker"]
CICD_WORKERS           = ["pipeline_config_worker", "environment_config_worker"]
DEPLOYMENT_WORKERS     = ["provisioner_worker", "health_check_worker"]

_INFRA_DEPS: Dict[str, List[str]] = {"dockerfile_writer_worker": [], "docker_compose_worker": []}
_CICD_DEPS: Dict[str, List[str]] = {"pipeline_config_worker": [], "environment_config_worker": []}
_DEPLOY_DEPS: Dict[str, List[str]] = {"provisioner_worker": [], "health_check_worker": ["provisioner_worker"]}


def build_devops_plan(project_id: str, feature_name: str, upstream_refs: Dict[str, Any]) -> DevOpsPlan:
    """Receive Manager Approval / Validate QA+Security -> the task graph for
    Generate Infrastructure + Generate CI/CD (pre-approval), and later
    Deploy + Health Check (post-approval)."""
    plan = DevOpsPlan(project_id=project_id, feature_name=feature_name, upstream_refs=upstream_refs)
    id_by_worker: Dict[str, str] = {}

    def _add(team: WorkerTeam, worker_id: str, deps_worker_ids: List[str]) -> None:
        task = DevOpsTask(
            project_id=project_id, team=team, worker_agent_id=worker_id,
            description=f"{team.value}:{worker_id} for feature '{feature_name}'",
            depends_on=[id_by_worker[d] for d in deps_worker_ids if d in id_by_worker],
        )
        id_by_worker[worker_id] = task.task_id
        plan.tasks.append(task)

    for w in INFRASTRUCTURE_WORKERS:
        _add(WorkerTeam.INFRASTRUCTURE, w, _INFRA_DEPS.get(w, []))
    for w in CICD_WORKERS:
        _add(WorkerTeam.CICD, w, _CICD_DEPS.get(w, []))
    for w in DEPLOYMENT_WORKERS:
        _add(WorkerTeam.DEPLOYMENT, w, _DEPLOY_DEPS.get(w, []))

    return plan


def topological_batches(tasks: List[DevOpsTask]) -> List[List[DevOpsTask]]:
    remaining = {t.task_id: t for t in tasks}
    done: Set[str] = set()
    batches: List[List[DevOpsTask]] = []

    while remaining:
        batch = [t for t in remaining.values() if all(d in done for d in t.depends_on)]
        if not batch:
            raise ValueError(f"Dependency cycle detected among DevOps tasks: {list(remaining.keys())}")
        batches.append(batch)
        for t in batch:
            done.add(t.task_id)
            del remaining[t.task_id]

    return batches


def team_progress(plan: DevOpsPlan, team: WorkerTeam) -> Dict[str, int]:
    team_tasks = plan.tasks_by_team(team)
    return {
        "total":     len(team_tasks),
        "completed": sum(1 for t in team_tasks if t.status.value == "completed"),
        "failed":    sum(1 for t in team_tasks if t.status.value == "failed"),
        "escalated": sum(1 for t in team_tasks if t.escalated),
    }


# -- Validate QA + Security (Stage 2 of the DevOps pipeline) --------

def validate_qa_and_security(qa_report: Optional[Dict[str, Any]], security_report: Optional[Dict[str, Any]]) -> List[str]:
    """
    Returns a list of blocking reasons (empty = clear to proceed).
    Per spec: DevOps consumes "Approved CodeModules, QA Report, Security
    Report ..." — a deployment plan can only be generated once both
    gates have passed (QA verdict != fail, Security verdict != fail;
    WARN is acceptable for both, matching QA/Security's own PASS/WARN/
    FAIL semantics).
    """
    reasons: List[str] = []
    if not qa_report:
        reasons.append("Missing QA report — cannot deploy unvalidated code")
    elif qa_report.get("verdict") == "fail":
        reasons.append(f"QA gate failed: {qa_report.get('blocking_conditions', [])}")

    if not security_report:
        reasons.append("Missing Security report — cannot deploy unvalidated code")
    elif security_report.get("verdict") == "fail":
        reasons.append(f"Security gate failed: {security_report.get('blocking_conditions', [])}")

    return reasons


def build_deployment_plan(
    project_id: str,
    qa_report: Optional[Dict[str, Any]],
    security_report: Optional[Dict[str, Any]],
    proposed_version: str,
    artifact_refs: Dict[str, Optional[str]],
    blocking_reasons: List[str],
) -> DeploymentPlan:
    return DeploymentPlan(
        project_id=project_id,
        proposed_version=proposed_version,
        qa_verdict=(qa_report or {}).get("verdict", "unknown"),
        security_verdict=(security_report or {}).get("verdict", "unknown"),
        risk_level=(security_report or {}).get("risk_level", "low"),
        dockerfile_ref=artifact_refs.get("dockerfile"),
        compose_ref=artifact_refs.get("docker_compose"),
        pipeline_ref=artifact_refs.get("pipeline_config"),
        environment_ref=artifact_refs.get("environment_config"),
        blocking_reasons=blocking_reasons,
    )


# -- Deterministic "Deployment Lead" — rollback decision --------------

def decide_rollback(health_report: HealthReport, deploy_succeeded: bool) -> Optional[str]:
    """
    Returns a rollback reason string if any of the spec's Rollback
    Policy triggers apply, else None. Deploy failure and health failure
    are the two triggers this pipeline can actually observe; the other
    four (startup_timeout, migration_failure, container_crash,
    dependency_unavailable) surface through the same health-check names
    (see models.REQUIRED_HEALTH_CHECKS / providers) rather than as
    separate signals, since this environment has no live infrastructure
    to independently detect them.
    """
    if not deploy_succeeded:
        return "deployment_failure: provider reported deploy failure"
    if not health_report.all_passed:
        return f"health_failure: {health_report.failed_checks}"
    return None


def build_health_report(project_id: str, deployment_id: str, raw_checks: List[Dict[str, Any]]) -> HealthReport:
    from services.devops.models import HealthCheckResult
    return HealthReport(
        project_id=project_id, deployment_id=deployment_id,
        checks=[HealthCheckResult(**c) for c in raw_checks],
    )


def build_rollback_report(project_id: str, deployment_id: str, reason: str,
                           rolled_back_to_version: Optional[str], succeeded: bool) -> RollbackReport:
    return RollbackReport(
        project_id=project_id, deployment_id=deployment_id, reason=reason,
        rolled_back_to_version=rolled_back_to_version,
        status=RollbackStatus.COMPLETED if succeeded else RollbackStatus.FAILED,
    )


# -- Deterministic "Release Lead" — version / notes / report ----------

def build_release(project_id: str, version: str, previous_version: Optional[str],
                   qa_report: Optional[Dict[str, Any]], security_report: Optional[Dict[str, Any]]) -> Release:
    notes_lines = [f"Release {version}"]
    if previous_version:
        notes_lines.append(f"Previous version: {previous_version}")
    if qa_report:
        notes_lines.append(f"QA: {qa_report.get('verdict', 'unknown')} "
                            f"({qa_report.get('tests_passed', 0)}/{qa_report.get('tests_total', 0)} tests)")
    if security_report:
        notes_lines.append(f"Security: {security_report.get('verdict', 'unknown')} "
                            f"(risk={security_report.get('risk_level', 'unknown')})")
    return Release(project_id=project_id, version=version, previous_version=previous_version,
                    release_notes="\n".join(notes_lines))


def build_deployment_report(
    project_id: str, deployment_id: str, status: DeploymentStatus,
    version: Optional[str], health_report: Optional[HealthReport],
    rollback_report: Optional[RollbackReport], release: Optional[Release],
    blocking_reasons: List[str],
) -> DeploymentReport:
    return DeploymentReport(
        project_id=project_id, deployment_id=deployment_id, status=status,
        version=version, health_report=health_report, rollback_report=rollback_report,
        release=release, blocking_reasons=blocking_reasons,
    )

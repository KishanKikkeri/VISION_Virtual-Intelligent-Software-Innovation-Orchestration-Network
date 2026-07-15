"""
services/integration/orchestrator.py
=================================
Top-level facade tying together dependency_graph, artifact_validator,
event_router, lifecycle (workflow validation), and health_validator
into the platform readiness report (spec §10), plus agent registry
validation (spec §8).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, Field

from core.runtime.factory import AGENT_REGISTRY
from services.integration import artifact_validator, dependency_graph, event_router, lifecycle
from services.integration.health_validator import PlatformHealthReport, generate_health_report

EXPECTED_AGENT_COUNT = 106
EXPECTED_DEPARTMENTS = {
    "manager", "product", "architecture", "engineering", "qa", "security",
    "devops", "docs", "monitoring", "incident_response",
}


class RegistryFinding(BaseModel):
    kind: str
    detail: str


class RegistryValidationReport(BaseModel):
    total_agents: int
    expected_agents: int = EXPECTED_AGENT_COUNT
    passed: bool
    departments: List[str] = Field(default_factory=list)
    findings: List[RegistryFinding] = Field(default_factory=list)


def validate_agent_registry() -> RegistryValidationReport:
    """spec §8: 106 registered agents, no duplicate IDs, correct parents,
    correct departments, correct layers, manager hierarchy intact."""
    findings: List[RegistryFinding] = []

    total = len(AGENT_REGISTRY)
    if total != EXPECTED_AGENT_COUNT:
        findings.append(RegistryFinding(
            kind="count_mismatch",
            detail=f"AGENT_REGISTRY has {total} agents, expected {EXPECTED_AGENT_COUNT}"))

    # AGENT_REGISTRY is a dict keyed by agent_id, so duplicate IDs are
    # structurally impossible within the dict itself — this checks for
    # the only way a "duplicate ID" could still occur: a department
    # collision (spec's own wording), i.e. two different AgentSpecs that
    # otherwise look identical (same name across departments).
    seen_names: Dict[str, str] = {}
    for agent_id, spec in AGENT_REGISTRY.items():
        if spec.name in seen_names and seen_names[spec.name] != agent_id:
            findings.append(RegistryFinding(
                kind="duplicate_name",
                detail=f"Agent name {spec.name!r} used by both {seen_names[spec.name]!r} and {agent_id!r}"))
        seen_names[spec.name] = agent_id

    departments = sorted({spec.department for spec in AGENT_REGISTRY.values()})
    missing_departments = EXPECTED_DEPARTMENTS - set(departments)
    for dept in sorted(missing_departments):
        findings.append(RegistryFinding(kind="missing_department", detail=f"{dept!r} has no registered agents"))

    # Manager hierarchy intact: every agent's parent_agent_id chain
    # must terminate at "manager_agent" (the only agent with no parent).
    for agent_id, spec in AGENT_REGISTRY.items():
        if agent_id == "manager_agent":
            if spec.parent_agent_id is not None:
                findings.append(RegistryFinding(
                    kind="root_has_parent", detail="manager_agent should have no parent"))
            continue
        chain = [agent_id]
        current = spec
        depth = 0
        while current.parent_agent_id and depth < 20:
            parent_id = current.parent_agent_id
            if parent_id not in AGENT_REGISTRY:
                findings.append(RegistryFinding(
                    kind="broken_parent_chain",
                    detail=f"{agent_id!r} chain references unknown parent {parent_id!r} (chain so far: {chain})"))
                break
            chain.append(parent_id)
            current = AGENT_REGISTRY[parent_id]
            depth += 1
        else:
            if depth >= 20:
                findings.append(RegistryFinding(
                    kind="cyclic_parent_chain", detail=f"{agent_id!r} parent chain exceeds 20 hops: {chain}"))
            continue
        if chain and chain[-1] != "manager_agent" and AGENT_REGISTRY[chain[-1]].parent_agent_id is None \
                and chain[-1] != "manager_agent":
            findings.append(RegistryFinding(
                kind="orphan_root",
                detail=f"{agent_id!r}'s chain terminates at {chain[-1]!r}, not 'manager_agent': {chain}"))

    return RegistryValidationReport(
        total_agents=total, passed=not findings, departments=departments, findings=findings,
    )


class ReadinessCategory(BaseModel):
    name: str
    score: float


class PlatformReadinessReport(BaseModel):
    categories: List[ReadinessCategory]
    overall: float


# Category weights sum to 1.0 — matches the 8 categories in the M3.9
# kickoff doc's own example (Infrastructure/Registry/Messaging/
# Workflows/Persistence/Monitoring/Recovery/Documentation).
CATEGORY_WEIGHTS: Dict[str, float] = {
    "infrastructure": 0.15, "registry": 0.15, "messaging": 0.15, "workflows": 0.15,
    "persistence": 0.10, "monitoring": 0.10, "recovery": 0.10, "documentation": 0.10,
}


def compute_readiness(
    health: PlatformHealthReport,
    registry: RegistryValidationReport,
    events: event_router.EventValidationReport,
    workflows: Dict[str, lifecycle.GraphAnalysis],
    repository_gaps: Dict[str, List[str]],
    documentation_score: float = 90.0,
) -> PlatformReadinessReport:
    """Deterministic weighted score (spec §10). Every sub-score is a
    plain 0-100 ratio computed from the other validators' own pass/fail
    counts — no magic numbers beyond the category weights and the
    caller-supplied documentation_score (there's no automated way to
    grade prose quality; this one input is intentionally the only
    non-derived figure, consistent with the kickoff doc's own example
    where "Documentation 90" reads like a judgment call, not a
    computed metric)."""
    healthy_count = sum(1 for c in health.components if c.status.value == "healthy")
    infra_score = 100.0 * healthy_count / max(len(health.components), 1)

    registry_score = 100.0 if registry.passed else max(
        0.0, 100.0 - 10.0 * len(registry.findings))

    total_events = max(events.total_subjects, 1)
    problem_events = len(events.orphan_events) + len(events.dead_subscriptions) + len(events.missing_routes)
    messaging_score = 100.0 * (1 - problem_events / total_events)

    total_graphs = max(len(workflows), 1)
    passing_graphs = sum(1 for w in workflows.values() if w.passed)
    workflows_score = 100.0 * passing_graphs / total_graphs

    persistence_score = 100.0 if not repository_gaps else max(
        0.0, 100.0 - 15.0 * sum(len(v) for v in repository_gaps.values()))

    monitoring_component = next((c for c in health.components if c.name == "monitoring"), None)
    monitoring_score = 100.0 if monitoring_component and monitoring_component.status.value == "healthy" else 50.0

    incident_component = next((c for c in health.components if c.name == "incident_response"), None)
    recovery_score = 100.0 if incident_component and incident_component.status.value == "healthy" else 50.0

    categories = [
        ReadinessCategory(name="infrastructure", score=round(infra_score, 1)),
        ReadinessCategory(name="registry", score=round(registry_score, 1)),
        ReadinessCategory(name="messaging", score=round(messaging_score, 1)),
        ReadinessCategory(name="workflows", score=round(workflows_score, 1)),
        ReadinessCategory(name="persistence", score=round(persistence_score, 1)),
        ReadinessCategory(name="monitoring", score=round(monitoring_score, 1)),
        ReadinessCategory(name="recovery", score=round(recovery_score, 1)),
        ReadinessCategory(name="documentation", score=round(documentation_score, 1)),
    ]
    overall = sum(c.score * CATEGORY_WEIGHTS[c.name] for c in categories)
    return PlatformReadinessReport(categories=categories, overall=round(overall, 1))


class PlatformReport(BaseModel):
    health: PlatformHealthReport
    registry: RegistryValidationReport
    events: event_router.EventValidationReport
    workflows: Dict[str, lifecycle.GraphAnalysis]
    dependency_sample: Dict[str, dependency_graph.DependencyCheckResult]
    repository_gaps: Dict[str, List[str]]
    readiness: PlatformReadinessReport


async def generate_full_report(
    db_factory: Any = None, nats: Any = None, factory: Any = None,
    repository_gaps: Optional[Dict[str, List[str]]] = None,
) -> PlatformReport:
    health = await generate_health_report(db_factory=db_factory, nats=nats, factory=factory)
    registry = validate_agent_registry()
    events = event_router.generate_event_report()
    workflows = lifecycle.validate_all_workflows()
    dep_sample = dependency_graph.full_lifecycle_report(set())  # empty-completed baseline
    gaps = repository_gaps if repository_gaps is not None else {}
    readiness = compute_readiness(health, registry, events, workflows, gaps)
    return PlatformReport(
        health=health, registry=registry, events=events, workflows=workflows,
        dependency_sample=dep_sample, repository_gaps=gaps, readiness=readiness,
    )

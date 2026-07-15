"""
services/integration/event_router.py
=================================
NATS subject manifest + validators (spec §6 Event Validation).

EVENT_MANIFEST below is **hand-verified against the actual source
code**, not derived from an automated AST scan — an automated scanner
over f-strings/prompt-example-strings/dynamic subjects is unreliable
enough to produce false positives (confirmed while building this: a
naive grep for quoted subject-like strings picked up JSON examples
inside LLM prompt templates in services/architecture/workers/
integration_architect.py and services/engineering/workers/
integration.py that are not real NATS activity at all). Every entry
below was confirmed by reading the actual `nats.publish(...)` /
`nats.subscribe(...)` call site.

This trades completeness (a new event added to the platform without a
manifest update won't be caught) for correctness (no false positives)
— documented explicitly here and in
docs/M3.9_Platform_Integration_Handover.md as this module's known
limitation. `WILDCARD_CONSUMERS` covers the four wildcard prefixes
Monitoring subscribes to for cross-department observability
(services/monitoring/api/events.py's `_WILDCARD_SUBJECTS`).
"""
from __future__ import annotations

from typing import Dict, List, Set

from pydantic import BaseModel, Field

# subject -> (publishers, consumers) — both as lists of "service.module" strings.
EVENT_MANIFEST: Dict[str, Dict[str, List[str]]] = {
    # ── Fully wired (publisher + consumer both confirmed) ──────────
    "product.requirements.completed":  {"publishers": ["product"], "consumers": ["manager"]},
    "architecture.design.completed":   {"publishers": ["architecture"], "consumers": ["engineering"]},
    "engineering.phase.completed":     {"publishers": ["engineering"], "consumers": ["qa", "security"]},
    "engineering.retry.completed":     {"publishers": ["engineering"], "consumers": ["qa", "security"]},
    "engineering.commit.requested":    {"publishers": ["engineering"], "consumers": ["repository"]},
    "engineering.pr.requested":        {"publishers": ["engineering"], "consumers": ["repository"]},
    "engineering.release.requested":   {"publishers": ["engineering"], "consumers": ["repository"]},
    "qa.phase.completed":              {"publishers": ["qa"], "consumers": ["devops"]},
    "security.phase.completed":        {"publishers": ["security"], "consumers": ["devops"]},
    "monitoring.incident":             {"publishers": ["monitoring"], "consumers": ["incident_response"]},
    "monitoring.alert":                {"publishers": ["monitoring"], "consumers": ["incident_response"]},
    "monitoring.warning":              {"publishers": ["monitoring"], "consumers": ["incident_response"]},

    # ── Orphan events (published, no consumer, no wildcard match) ──
    "architecture.approval.requested":      {"publishers": ["architecture"], "consumers": []},
    "architecture.pipeline.failed":         {"publishers": ["architecture"], "consumers": []},
    "architecture.platform_design.completed": {"publishers": ["architecture"], "consumers": []},
    "architecture.system_design.completed": {"publishers": ["architecture"], "consumers": []},
    "engineering.pipeline.failed":          {"publishers": ["engineering"], "consumers": []},
    "engineering.plan.created":             {"publishers": ["engineering"], "consumers": []},
    "engineering.tasks.dead_lettered":       {"publishers": ["engineering"], "consumers": []},
    "qa.phase.started":                     {"publishers": ["qa"], "consumers": []},
    "qa.phase.failed":                      {"publishers": ["qa"], "consumers": []},
    "qa.coverage.completed":                {"publishers": ["qa"], "consumers": []},
    "security.phase.started":               {"publishers": ["security"], "consumers": []},
    "security.phase.failed":                {"publishers": ["security"], "consumers": []},
    "security.findings.created":            {"publishers": ["security"], "consumers": []},
    "security.scan.completed":              {"publishers": ["security"], "consumers": []},
    "devops.phase.started":                 {"publishers": ["devops"], "consumers": []},
    "devops.phase.failed":                  {"publishers": ["devops"], "consumers": []},
    "devops.phase.completed":               {"publishers": ["devops"], "consumers": []},
    "rollback.completed":                   {"publishers": ["devops"], "consumers": []},
    "deployment.completed":                 {"publishers": ["devops"], "consumers": []},
    "deployment.failed":                    {"publishers": ["devops"], "consumers": []},
    "health.completed":                     {"publishers": ["devops"], "consumers": []},
    "monitoring.phase.completed":           {"publishers": ["monitoring"], "consumers": []},
    "monitoring.metrics.updated":           {"publishers": ["monitoring"], "consumers": []},
    "incident.notification":                {"publishers": ["incident_response"], "consumers": []},
    "incident.phase.completed":             {"publishers": ["incident_response"], "consumers": []},
    "incident.resolved":                    {"publishers": ["incident_response"], "consumers": []},
    "incident.rollback.requested":          {"publishers": ["incident_response"], "consumers": []},
    "incident.updated":                     {"publishers": ["incident_response"], "consumers": []},

    # ── Namespace mismatch: published un-namespaced, so they never
    #    actually match Monitoring's "repository.>" wildcard even
    #    though a human reading RepositoryEventType would assume they
    #    do (see docs/M3.9_Platform_Integration_Handover.md §Findings) ──
    "branch.created":   {"publishers": ["repository"], "consumers": []},
    "commit.created":   {"publishers": ["repository"], "consumers": []},
    "pr.created":       {"publishers": ["repository"], "consumers": []},
    "pr.approved":      {"publishers": ["repository"], "consumers": []},
    "release.created":  {"publishers": ["repository"], "consumers": []},

    # ── Likely-missing routes: these carry "retry"/"defect" semantics
    #    that strongly suggest an intended consumer (Engineering,
    #    to close the rework loop) that was never wired ──
    "qa.defect.created":       {"publishers": ["qa"], "consumers": [], "likely_missing_route_to": "engineering"},
    "qa.retry.requested":      {"publishers": ["qa"], "consumers": [], "likely_missing_route_to": "engineering"},
    "security.retry.requested": {"publishers": ["security"], "consumers": [], "likely_missing_route_to": "engineering"},

    # ── Dead subscription: DevOps subscribes, nothing ever publishes ──
    "manager.deploy.approved": {"publishers": [], "consumers": ["devops"]},

    # Namespaced repository events that DO match the repository.> wildcard.
    "repository.created":            {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
    "repository.workflow.completed": {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
}

# Wildcard prefixes Monitoring subscribes to for cross-department
# observability (services/monitoring/api/events.py's _WILDCARD_SUBJECTS).
# Any concrete subject matching one of these has an implicit consumer
# even if not listed explicitly in EVENT_MANIFEST.
WILDCARD_CONSUMERS: Dict[str, str] = {
    "repository.": "monitoring",
    "manager.":    "monitoring",
    "agent.":      "monitoring",
    "system.":     "monitoring",
}


class EventFinding(BaseModel):
    subject: str
    kind: str    # "orphan" | "dead_subscription" | "namespace_mismatch" | "missing_route"
    detail: str


class EventValidationReport(BaseModel):
    total_subjects: int
    orphan_events: List[EventFinding] = Field(default_factory=list)
    dead_subscriptions: List[EventFinding] = Field(default_factory=list)
    duplicate_consumers: List[EventFinding] = Field(default_factory=list)
    missing_routes: List[EventFinding] = Field(default_factory=list)
    namespace_mismatches: List[EventFinding] = Field(default_factory=list)
    healthy_subjects: List[str] = Field(default_factory=list)


def _matches_wildcard(subject: str) -> bool:
    return any(subject.startswith(prefix) for prefix in WILDCARD_CONSUMERS)


def find_orphan_events() -> List[EventFinding]:
    """Published, zero consumers, and no wildcard match."""
    out = []
    for subject, entry in EVENT_MANIFEST.items():
        if entry.get("publishers") and not entry.get("consumers") and not _matches_wildcard(subject):
            if "likely_missing_route_to" in entry:
                continue  # classified separately, see find_missing_routes()
            out.append(EventFinding(subject=subject, kind="orphan",
                       detail=f"Published by {entry['publishers']}, no known consumer."))
    return out


def find_dead_subscriptions() -> List[EventFinding]:
    """Subscribed, but zero known publishers."""
    return [
        EventFinding(subject=subject, kind="dead_subscription",
                     detail=f"Subscribed by {entry['consumers']}, no known publisher.")
        for subject, entry in EVENT_MANIFEST.items()
        if entry.get("consumers") and not entry.get("publishers")
    ]


def find_duplicate_consumers() -> List[EventFinding]:
    """More than one consumer for the same subject — reported factually;
    not necessarily a bug (engineering.phase.completed fanning out to
    both QA and Security is intentional parallel-tier dispatch, spec's
    own PHASE_TIERS)."""
    return [
        EventFinding(subject=subject, kind="duplicate_consumers",
                     detail=f"{len(entry['consumers'])} consumers: {entry['consumers']} "
                            f"(intentional fan-out unless noted otherwise).")
        for subject, entry in EVENT_MANIFEST.items()
        if len(entry.get("consumers", [])) > 1
    ]


def find_missing_routes() -> List[EventFinding]:
    return [
        EventFinding(subject=subject, kind="missing_route",
                     detail=f"Published by {entry['publishers']}, no consumer, but subject name "
                            f"suggests an intended route to {entry['likely_missing_route_to']!r}.")
        for subject, entry in EVENT_MANIFEST.items()
        if "likely_missing_route_to" in entry
    ]


def find_namespace_mismatches() -> List[EventFinding]:
    """Subjects published un-namespaced that a reasonable reading of a
    sibling wildcard subscription (e.g. Monitoring's `repository.>`)
    would expect to match, but don't."""
    out = []
    for subject, entry in EVENT_MANIFEST.items():
        if subject in ("branch.created", "commit.created", "pr.created", "pr.approved", "release.created"):
            out.append(EventFinding(
                subject=subject, kind="namespace_mismatch",
                detail=f"Published un-namespaced by {entry['publishers']}; does not match "
                        f"any 'repository.>'-style wildcard consumer despite sibling events "
                        f"like 'repository.created' being properly namespaced."))
    return out


def generate_event_report() -> EventValidationReport:
    orphans = find_orphan_events()
    dead = find_dead_subscriptions()
    dupes = find_duplicate_consumers()
    missing = find_missing_routes()
    mismatches = find_namespace_mismatches()
    flagged_subjects = {f.subject for f in orphans + dead + missing + mismatches}
    healthy = [s for s in EVENT_MANIFEST if s not in flagged_subjects]
    return EventValidationReport(
        total_subjects=len(EVENT_MANIFEST),
        orphan_events=orphans, dead_subscriptions=dead,
        duplicate_consumers=dupes, missing_routes=missing,
        namespace_mismatches=mismatches, healthy_subjects=sorted(healthy),
    )

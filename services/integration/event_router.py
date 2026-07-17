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
limitation.

**Messaging cleanup pass** (see the M3.9 handover's follow-up notes):
1. Monitoring's wildcard subscription set (services/monitoring/api/
   events.py's `_WILDCARD_SUBJECTS`) was extended from 4 prefixes to
   10 — one `.>` wildcard per department — which resolved the large
   majority of the orphan events originally found (every
   `*.phase.completed`/`*.phase.started`/`*.phase.failed`, QA's
   `qa.defect.created`/`qa.retry.requested`, Security's
   `security.retry.requested`, and every Incident Response event).
   These are consumed purely for cross-department observability, not
   to drive control flow — Manager's own
   `engineering_rework_node` already handles the QA/Security rework
   loop synchronously from the delegation result, so a dedicated
   Engineering-side NATS consumer for the retry/defect events would
   have been redundant, not a real fix.
2. Repository Service's `branch.created`/`commit.created`/
   `pr.created`/`pr.approved`/`pr.merged`/`release.created`/
   `release.rollback` events (services/repository/schemas/
   __init__.py's `RepositoryEventType` enum — the single source of
   truth `AuditManager.record_and_publish()` uses for every publish)
   are now namespaced under `repository.*`, so they actually match
   Monitoring's pre-existing `repository.>` wildcard the way a human
   reading the code would already have assumed they did.
3. DevOps's dead `manager.deploy.approved` subscription (nothing in
   the platform ever published it) was removed from
   services/devops/api/events.py entirely, rather than half-building
   the "standalone deployment mode" publish side that subscription's
   own docstring gestured at — that's a bigger change than a
   messaging-cleanup pass warrants.

**Left un-namespaced, out of this cleanup's explicit scope**: DevOps's
own `rollback.completed`/`deployment.completed`/`deployment.failed`/
`health.completed` have the exact same un-namespaced-orphan pattern
Repository's events had, but only Repository's events were in scope
for this pass. They remain genuine orphans (see
`find_namespace_mismatches()` / `_UNNAMESPACED_DEVOPS_EVENTS` below)
and are flagged again here as the obvious next follow-up.
"""
from __future__ import annotations

from typing import Dict, List, Set

from pydantic import BaseModel, Field

# subject -> (publishers, consumers) — both as lists of "service.module" strings.
EVENT_MANIFEST: Dict[str, Dict[str, List[str]]] = {
    # ── Fully wired (publisher + direct consumer both confirmed) ───
    "product.requirements.completed":  {"publishers": ["product"], "consumers": ["manager"]},
    "architecture.design.completed":   {"publishers": ["architecture"], "consumers": ["engineering", "monitoring (wildcard)"]},
    "engineering.phase.completed":     {"publishers": ["engineering"], "consumers": ["qa", "security", "monitoring (wildcard)"]},
    "engineering.retry.completed":     {"publishers": ["engineering"], "consumers": ["qa", "security", "monitoring (wildcard)"]},
    "engineering.commit.requested":    {"publishers": ["engineering"], "consumers": ["repository"]},
    "engineering.pr.requested":        {"publishers": ["engineering"], "consumers": ["repository"]},
    "engineering.release.requested":   {"publishers": ["engineering"], "consumers": ["repository"]},
    "qa.phase.completed":              {"publishers": ["qa"], "consumers": ["devops", "monitoring (wildcard)"]},
    "security.phase.completed":        {"publishers": ["security"], "consumers": ["devops", "monitoring (wildcard)"]},
    "monitoring.incident":             {"publishers": ["monitoring"], "consumers": ["incident_response"]},
    "monitoring.alert":                {"publishers": ["monitoring"], "consumers": ["incident_response"]},
    "monitoring.warning":              {"publishers": ["monitoring"], "consumers": ["incident_response"]},

    # ── Resolved by the messaging cleanup pass: now consumed via
    #    Monitoring's per-department wildcard (observability only —
    #    see module docstring for why that's the correct fix here,
    #    not a workaround) ──────────────────────────────────────────
    "architecture.approval.requested":        {"publishers": ["architecture"], "consumers": ["monitoring (wildcard)"]},
    "architecture.pipeline.failed":           {"publishers": ["architecture"], "consumers": ["monitoring (wildcard)"]},
    "architecture.platform_design.completed": {"publishers": ["architecture"], "consumers": ["monitoring (wildcard)"]},
    "architecture.system_design.completed":   {"publishers": ["architecture"], "consumers": ["monitoring (wildcard)"]},
    "engineering.pipeline.failed":            {"publishers": ["engineering"], "consumers": ["monitoring (wildcard)"]},
    "engineering.plan.created":               {"publishers": ["engineering"], "consumers": ["monitoring (wildcard)"]},
    "engineering.tasks.dead_lettered":        {"publishers": ["engineering"], "consumers": ["monitoring (wildcard)"]},
    "qa.phase.started":                       {"publishers": ["qa"], "consumers": ["monitoring (wildcard)"]},
    "qa.phase.failed":                        {"publishers": ["qa"], "consumers": ["monitoring (wildcard)"]},
    "qa.coverage.completed":                  {"publishers": ["qa"], "consumers": ["monitoring (wildcard)"]},
    "qa.defect.created":                      {"publishers": ["qa"], "consumers": ["monitoring (wildcard)"]},
    "qa.retry.requested":                     {"publishers": ["qa"], "consumers": ["monitoring (wildcard)"]},
    "security.phase.started":                 {"publishers": ["security"], "consumers": ["monitoring (wildcard)"]},
    "security.phase.failed":                  {"publishers": ["security"], "consumers": ["monitoring (wildcard)"]},
    "security.findings.created":              {"publishers": ["security"], "consumers": ["monitoring (wildcard)"]},
    "security.scan.completed":                {"publishers": ["security"], "consumers": ["monitoring (wildcard)"]},
    "security.retry.requested":               {"publishers": ["security"], "consumers": ["monitoring (wildcard)"]},
    "devops.phase.started":                   {"publishers": ["devops"], "consumers": ["monitoring (wildcard)"]},
    "devops.phase.failed":                     {"publishers": ["devops"], "consumers": ["monitoring (wildcard)"]},
    "devops.phase.completed":                  {"publishers": ["devops"], "consumers": ["monitoring (wildcard)"]},
    "monitoring.phase.completed":              {"publishers": ["monitoring"], "consumers": []},  # Monitoring doesn't sub to itself, by design
    "monitoring.metrics.updated":              {"publishers": ["monitoring"], "consumers": []},  # ditto
    "incident.notification":                   {"publishers": ["incident_response"], "consumers": ["monitoring (wildcard)"]},
    "incident.phase.completed":                {"publishers": ["incident_response"], "consumers": ["monitoring (wildcard)"]},
    "incident.resolved":                       {"publishers": ["incident_response"], "consumers": ["monitoring (wildcard)"]},
    "incident.rollback.requested":              {"publishers": ["incident_response"], "consumers": ["monitoring (wildcard)"]},
    "incident.updated":                        {"publishers": ["incident_response"], "consumers": ["monitoring (wildcard)"]},

    # ── Resolved by the messaging cleanup pass: Repository's events
    #    are now namespaced (repository.branch.created, etc.) and so
    #    match the repository.> wildcard Monitoring already had ──────
    "repository.branch.created":   {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
    "repository.commit.created":   {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
    "repository.pr.created":       {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
    "repository.pr.approved":      {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
    "repository.pr.merged":        {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
    "repository.release.created":  {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
    "repository.release.rollback": {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
    "repository.created":            {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},
    "repository.workflow.completed": {"publishers": ["repository"], "consumers": ["monitoring (wildcard)"]},

    # ── Explicitly NOT in scope for this cleanup pass (see module
    #    docstring): DevOps publishes these un-namespaced, the exact
    #    same pattern Repository had, but only Repository was asked
    #    for by name. Flagged again as the obvious next follow-up. ──
    "rollback.completed":   {"publishers": ["devops"], "consumers": []},
    "deployment.completed": {"publishers": ["devops"], "consumers": []},
    "deployment.failed":    {"publishers": ["devops"], "consumers": []},
    "health.completed":     {"publishers": ["devops"], "consumers": []},
}

# DevOps events left un-namespaced on purpose (out of this cleanup
# pass's explicit scope) — see EVENT_MANIFEST's trailing block above.
_UNNAMESPACED_DEVOPS_EVENTS: Set[str] = {
    "rollback.completed", "deployment.completed", "deployment.failed", "health.completed",
}

# Wildcard prefixes Monitoring subscribes to for cross-department
# observability (services/monitoring/api/events.py's _WILDCARD_SUBJECTS,
# expanded during the M3.9 messaging cleanup from 4 prefixes to 10).
# Any concrete subject matching one of these has an implicit consumer
# even if not listed explicitly in EVENT_MANIFEST.
WILDCARD_CONSUMERS: Dict[str, str] = {
    "architecture.": "monitoring",
    "engineering.":  "monitoring",
    "qa.":           "monitoring",
    "security.":     "monitoring",
    "devops.":       "monitoring",
    "incident.":     "monitoring",
    "repository.":   "monitoring",
    "manager.":      "monitoring",
    "agent.":        "monitoring",
    "system.":       "monitoring",
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
    return [
        EventFinding(subject=subject, kind="orphan",
                     detail=f"Published by {entry['publishers']}, no known consumer.")
        for subject, entry in EVENT_MANIFEST.items()
        if entry.get("publishers") and not entry.get("consumers") and not _matches_wildcard(subject)
    ]


def find_dead_subscriptions() -> List[EventFinding]:
    """Subscribed, but zero known publishers."""
    return [
        EventFinding(subject=subject, kind="dead_subscription",
                     detail=f"Subscribed by {entry['consumers']}, no known publisher.")
        for subject, entry in EVENT_MANIFEST.items()
        if entry.get("consumers") and not entry.get("publishers")
    ]


def find_duplicate_consumers() -> List[EventFinding]:
    """More than one *direct* (non-wildcard) consumer for the same
    subject — reported factually; not necessarily a bug
    (engineering.phase.completed fanning out to both QA and Security is
    intentional parallel-tier dispatch, spec's own PHASE_TIERS). The
    ubiquitous "monitoring (wildcard)" consumer is excluded from this
    count — nearly every subject has it now, so counting it here would
    make the finding meaningless.
    """
    return [
        EventFinding(subject=subject, kind="duplicate_consumers",
                     detail=f"{len(direct)} direct consumers: {direct} "
                            f"(intentional fan-out unless noted otherwise).")
        for subject, entry in EVENT_MANIFEST.items()
        for direct in [[c for c in entry.get("consumers", []) if "wildcard" not in c]]
        if len(direct) > 1
    ]


def find_missing_routes() -> List[EventFinding]:
    """Kept for the interface's sake — the messaging cleanup pass
    resolved every subject that used to be classified this way
    (qa.defect.created, qa.retry.requested, security.retry.requested)
    by giving them a real (observability) consumer via Monitoring's
    wildcard, since the rework loop they might have suggested is
    actually already handled synchronously by Manager's own
    engineering_rework_node — see module docstring. Always empty
    today; the function stays so a *future* genuinely-missing route
    has somewhere to be reported."""
    return [
        EventFinding(subject=subject, kind="missing_route",
                     detail=f"Published by {entry['publishers']}, no consumer at all "
                            f"(not even the wildcard), and the name suggests one was intended.")
        for subject, entry in EVENT_MANIFEST.items()
        if entry.get("publishers") and not entry.get("consumers") and not _matches_wildcard(subject)
        and subject in _LIKELY_INTENTIONAL_ROUTE_NAMES
    ]


_LIKELY_INTENTIONAL_ROUTE_NAMES: Set[str] = set()  # empty post-cleanup; see find_missing_routes()


def find_namespace_mismatches() -> List[EventFinding]:
    """Subjects published un-namespaced that a reasonable reading of a
    sibling wildcard subscription (e.g. Monitoring's `devops.>`) would
    expect to match, but don't. Post-cleanup, this is exactly (and
    only) DevOps's 4 remaining un-namespaced events — Repository's own
    equivalent issue was fixed (see module docstring)."""
    return [
        EventFinding(
            subject=subject, kind="namespace_mismatch",
            detail=f"Published un-namespaced by {entry['publishers']}; does not match "
                    f"the 'devops.>' wildcard consumer despite sibling events like "
                    f"'devops.phase.completed' being properly namespaced. Same class of "
                    f"issue Repository Service had before the M3.9 messaging cleanup; "
                    f"deliberately left out of that pass's scope.")
        for subject, entry in EVENT_MANIFEST.items()
        if subject in _UNNAMESPACED_DEVOPS_EVENTS
    ]


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

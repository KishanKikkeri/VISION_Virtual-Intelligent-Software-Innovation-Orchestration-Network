"""
AASC — LangGraph Workflow Definitions v1
=========================================
Project  : Autonomous AI Software Company
Status   : Locked
Date     : 2026-06-08
Graphs   : 12
Nodes    : ~130 (stubs — implementations are in the Agent Runtime layer)
Next doc : Agent Runtime Specification v1

──────────────────────────────────────────────────────────────
DESIGN RULES (non-negotiable)
──────────────────────────────────────────────────────────────
R1  Every workflow is durable via PostgresSaver checkpointing.
    Server crash at node 17/40 → resumes at node 17, not start.

R2  Human approval gates are first-class interrupt nodes.
    They are compiled with interrupt_before=[...] — not if-statements.
    Workflows may pause for days at these nodes.

R3  Only manager-service writes to current_phase and phase_status.
    Workers produce outputs and update their own sub-state only.

R4  Parallelism by department: QA, Security, and Docs run via
    LangGraph Send() fan-out, not sequential execution.

R5  Every node defines four paths: success, failure, retry, escalation.
    No dead ends. Every failure eventually reaches a recovery decision.

──────────────────────────────────────────────────────────────
WORKFLOW INDEX
──────────────────────────────────────────────────────────────
W01  Project Lifecycle Graph      Master graph. Manager-owned.
W02  Product Service Graph        Idea → Requirements + User Stories
W03  Architecture Service Graph   Requirements → Architecture Blueprint
W04  Engineering Graph            Blueprint → Committed Code (4 stages)
W05  QA Graph                     Code → Test Reports + Coverage
W06  Security Graph               Code → Security Report (parallel to QA)
W07  Documentation Graph          Ongoing → Docs artifacts (non-blocking)
W08  DevOps Graph                 Validated code → Deployed application
W09  Monitoring Graph             Always-on production health watch
W10  Incident Response Graph      Incident → Root Cause → Patch → Deploy
W11  Cost Protection Graph        Per agent-run budget enforcement
W12  Task Delegation Graph        The brain: task → agent → model → result
"""

from __future__ import annotations
from typing import TypedDict, List, Optional, Dict, Literal, Any, Annotated
from langgraph.graph import StateGraph, END
from langgraph.types import Send
from langgraph.checkpoint.postgres import PostgresSaver


# ═══════════════════════════════════════════════════════════════
# SECTION 1 — STATE DEFINITIONS
# One TypedDict per workflow. ProjectState is the master.
# Sub-states are slices passed into sub-graphs.
# Only manager-service writes to ProjectState.current_phase.
# ═══════════════════════════════════════════════════════════════

class ProjectState(TypedDict):
    """
    Master state for W01 (Project Lifecycle Graph).
    Manager Agent is the sole writer of phase-level fields.
    All other fields are read-only from sub-service perspectives.
    """
    # ── Identity ──────────────────────────────────────────────
    project_id:             str
    workflow_id:            str
    owner_id:               str

    # ── Phase tracking (manager writes only) ──────────────────
    current_phase:          int            # 1–10
    phase_status:           str            # pending|running|awaiting_approval|completed|failed

    # ── Task tracking ─────────────────────────────────────────
    active_tasks:           List[Dict[str, Any]]
    completed_tasks:        List[Dict[str, Any]]
    failed_tasks:           List[Dict[str, Any]]

    # ── Artifacts (artifact_type → artifact_id) ───────────────
    artifacts:              Dict[str, str]

    # ── Approval gate state ───────────────────────────────────
    awaiting_approval:      bool
    approval_artifact_type: Optional[str]
    approval_status:        Optional[str]  # pending|approved|rejected|expired
    approval_feedback:      Optional[str]
    revision_round:         int

    # ── Budget / cost ─────────────────────────────────────────
    budget_limit_usd:       Optional[float]  # None = unlimited
    total_spend_usd:        float
    budget_status:          str              # active|warning|exceeded

    # ── Error handling ────────────────────────────────────────
    retry_count:            int
    max_retries:            int              # default: 3
    failure_reason:         Optional[str]
    escalation_required:    bool
    dead_lettered_tasks:    List[str]

    # ── Event queues (flushed at end of each node) ────────────
    nats_events_queue:      List[Dict[str, Any]]
    websocket_events_queue: List[Dict[str, Any]]


class ProductState(TypedDict):
    """State for W02 — Product Service Graph."""
    project_id:          str
    workflow_id:         str
    project_description: str
    features:            List[Dict[str, Any]]
    requirements:        List[Dict[str, Any]]
    user_stories:        List[Dict[str, Any]]
    acceptance_criteria: List[Dict[str, Any]]
    artifacts:           Dict[str, str]
    review_passed:       bool
    awaiting_approval:   bool
    approval_status:     Optional[str]
    approval_feedback:   Optional[str]
    revision_round:      int
    revision_feedback:   Optional[str]    # injected from approval rejection
    retry_count:         int
    failure_reason:      Optional[str]


class ArchitectureState(TypedDict):
    """State for W03 — Architecture Service Graph."""
    project_id:                  str
    workflow_id:                 str
    requirements_artifact_id:    str
    system_design:               Optional[Dict[str, Any]]
    api_specification:           Optional[Dict[str, Any]]
    db_schema:                   Optional[Dict[str, Any]]
    infrastructure_plan:         Optional[Dict[str, Any]]
    traceability_check_passed:   bool
    artifacts:                   Dict[str, str]
    review_passed:               bool
    awaiting_approval:           bool
    approval_status:             Optional[str]
    approval_feedback:           Optional[str]
    revision_round:              int
    retry_count:                 int
    failure_reason:              Optional[str]


class EngineeringState(TypedDict):
    """State for W04 — Engineering Graph."""
    project_id:           str
    workflow_id:          str
    architecture_artifact_id: str
    task_plan:            List[Dict[str, Any]]   # [{task_id, layer, description, depends_on[]}]
    active_modules:       List[str]               # module_ids
    completed_modules:    List[str]
    failed_modules:       List[str]
    backend_complete:     bool
    frontend_complete:    bool
    integration_complete: bool
    all_modules_committed:bool
    artifacts:            Dict[str, str]
    retry_count:          int
    failure_reason:       Optional[str]


class QAState(TypedDict):
    """State for W05 — QA Graph."""
    project_id:               str
    workflow_id:              str
    modules_to_test:          List[str]
    unit_test_results:        Optional[Dict[str, Any]]
    integration_test_results: Optional[Dict[str, Any]]
    regression_test_results:  Optional[Dict[str, Any]]
    performance_test_results: Optional[Dict[str, Any]]
    coverage_report:          Optional[Dict[str, Any]]
    overall_status:           str       # pending|passed|failed
    blocking_failures:        List[str]
    artifacts:                Dict[str, str]


class SecurityState(TypedDict):
    """State for W06 — Security Graph."""
    project_id:             str
    workflow_id:            str
    dependency_scan:        Optional[Dict[str, Any]]
    code_scan:              Optional[Dict[str, Any]]
    secret_scan:            Optional[Dict[str, Any]]
    compliance_results:     Optional[Dict[str, Any]]
    findings:               List[Dict[str, Any]]
    critical_count:         int
    high_count:             int
    medium_count:           int
    overall_status:         str      # pending|passed|warning|blocked
    blocks_deployment:      bool
    artifacts:              Dict[str, str]


class DocsState(TypedDict):
    """State for W07 — Documentation Graph."""
    project_id:    str
    workflow_id:   str
    api_docs:      Optional[str]
    readme:        Optional[str]
    user_guide:    Optional[str]
    changelog:     Optional[str]
    artifacts:     Dict[str, str]
    failure_reason:Optional[str]   # warnings only — never blocks pipeline


class DevOpsState(TypedDict):
    """State for W08 — DevOps Graph."""
    project_id:         str
    workflow_id:        str
    qa_passed:          bool
    security_passed:    bool
    dockerfiles:        Dict[str, str]   # service_name → dockerfile content
    docker_compose:     Optional[str]
    cicd_config:        Optional[str]
    env_templates:      Optional[str]
    deployment_plan:    Optional[Dict[str, Any]]
    awaiting_approval:  bool
    approval_status:    Optional[str]
    deployment_status:  str              # pending|running|completed|failed|rolled_back
    health_check_passed:bool
    rollback_triggered: bool
    artifacts:          Dict[str, str]


class MonitoringState(TypedDict):
    """State for W09 — Monitoring Graph (always active post-deployment)."""
    project_id:        str
    metrics_snapshot:  Optional[Dict[str, Any]]
    anomalies:         List[Dict[str, Any]]
    severity:          str    # none|low|medium|high
    incident_triggered:bool
    ticket_created:    bool
    manager_notified:  bool


class IncidentState(TypedDict):
    """State for W10 — Incident Response Graph."""
    project_id:          str
    incident_id:         str
    incident_description:str
    logs:                Optional[Dict[str, Any]]
    root_cause:          Optional[str]
    fix_proposal:        Optional[str]
    qa_passed:           bool
    awaiting_approval:   bool
    approval_status:     Optional[str]
    patch_deployed:      bool
    new_version:         Optional[str]
    failure_reason:      Optional[str]


class CostProtectionState(TypedDict):
    """
    State for W11 — Cost Protection Graph.
    Triggered after every LLM call by any agent.
    """
    project_id:         str
    agent_run_id:       str
    agent_id:           str
    department:         str
    provider:           str
    model:              str
    input_tokens:       int
    output_tokens:      int
    cost_usd:           float
    project_total_spend:float
    budget_limit_usd:   Optional[float]
    budget_status:      str    # active|warning|exceeded


class TaskDelegationState(TypedDict):
    """
    State for W12 — Task Delegation Graph.
    The brain: routes tasks → departments → agents → models.
    Owns retry decisions, escalation, and dead-letter routing.
    """
    project_id:       str
    task_id:          str
    task_type:        str
    task_description: str
    task_context:     Dict[str, Any]   # relevant artifacts, constraints, prior feedback
    task_priority:    int              # 1 (highest) to 10 (lowest)
    department:       Optional[str]
    selected_agent:   Optional[str]
    selected_provider:Optional[str]
    selected_model:   Optional[str]
    agent_run_id:     Optional[str]
    task_status:      str   # pending|assigned|running|completed|failed|escalated|dead_lettered
    task_output:      Optional[Dict[str, Any]]
    validation_passed:bool
    retry_count:      int
    max_retries:      int   # default: 3
    escalation_level: int   # 0=worker, 1=lead, 2=head, 3=manager, 4=user
    dead_lettered:    bool
    failure_reason:   Optional[str]


# ═══════════════════════════════════════════════════════════════
# SECTION 2 — ROUTING FUNCTIONS
# Pure functions. No side effects. Return only the route string.
# ═══════════════════════════════════════════════════════════════

# ── W01 routers ───────────────────────────────────────────────

def route_requirements_approval(state: ProjectState) -> str:
    if state["budget_status"] == "exceeded":        return "budget_exceeded"
    if state["revision_round"] >= 5:                return "max_revisions"
    if state["approval_status"] == "approved":      return "approved"
    if state["approval_status"] == "rejected":      return "rejected"
    return "pending"

def route_architecture_approval(state: ProjectState) -> str:
    if state["budget_status"] == "exceeded":        return "budget_exceeded"
    if state["revision_round"] >= 5:                return "max_revisions"
    if state["approval_status"] == "approved":      return "approved"
    if state["approval_status"] == "rejected":      return "rejected"
    return "pending"

def route_implementation(state: ProjectState) -> str:
    if state["budget_status"] == "exceeded":        return "budget_exceeded"
    if state["phase_status"] == "failed":           return "failed"
    if state["phase_status"] == "completed":        return "complete"
    return "complete"

def route_validation(state: ProjectState) -> str:
    """Routes after QA + Security parallel phase."""
    if state["budget_status"] == "exceeded":        return "budget_exceeded"
    # security blocking findings prevent any further progress
    security_blocked = any(
        t.get("type") == "security_block" for t in state["failed_tasks"]
    )
    if security_blocked:                            return "security_blocked"
    qa_failed = any(
        t.get("type") == "qa_failure" for t in state["failed_tasks"]
    )
    if qa_failed:                                   return "qa_failed"
    return "all_passed"

def route_deployment_approval(state: ProjectState) -> str:
    if state["budget_status"] == "exceeded":        return "budget_exceeded"
    if state["approval_status"] == "approved":      return "approved"
    if state["approval_status"] == "rejected":      return "rejected"
    return "pending"

def route_after_deployment(state: ProjectState) -> str:
    if state["phase_status"] == "failed":           return "failed"
    return "success"

# ── W02 routers ───────────────────────────────────────────────

def route_product_lead_review(state: ProductState) -> str:
    if state["retry_count"] >= 3:                   return "escalate"
    if state["review_passed"]:                      return "passed"
    return "failed"

def route_product_approval(state: ProductState) -> str:
    if state["revision_round"] >= 5:                return "max_revisions"
    if state["approval_status"] == "approved":      return "approved"
    if state["approval_status"] == "rejected":      return "rejected"
    return "pending"

# ── W03 routers ───────────────────────────────────────────────

def route_architecture_review(state: ArchitectureState) -> str:
    if state["retry_count"] >= 3:                   return "escalate"
    if not state["traceability_check_passed"]:      return "traceability_gap"
    if state["review_passed"]:                      return "passed"
    return "failed"

def route_architecture_approval_gate(state: ArchitectureState) -> str:
    if state["revision_round"] >= 5:                return "max_revisions"
    if state["approval_status"] == "approved":      return "approved"
    if state["approval_status"] == "rejected":      return "rejected"
    return "pending"

# ── W04 routers ───────────────────────────────────────────────

def route_module_review(state: Dict[str, Any]) -> str:
    """Per-module review decision."""
    if state.get("retry_count", 0) >= 3:            return "escalate"
    if state.get("review_passed"):                  return "approved"
    return "revision_needed"

def route_engineering_completion(state: EngineeringState) -> str:
    if state["failed_modules"] and not state["completed_modules"]: return "all_failed"
    if not state["all_modules_committed"]:          return "partial"
    return "complete"

# ── W05 routers ───────────────────────────────────────────────

def route_qa_decision(state: QAState) -> str:
    if state["blocking_failures"]:                  return "failed"
    if state["overall_status"] == "passed":         return "passed"
    return "failed"

# ── W06 routers ───────────────────────────────────────────────

def route_security_decision(state: SecurityState) -> str:
    if state["critical_count"] > 0:                 return "blocked"
    if state["high_count"] > 0:                     return "blocked"
    if state["medium_count"] > 0:                   return "warning"
    return "passed"

# ── W08 routers ───────────────────────────────────────────────

def route_prerequisites_check(state: DevOpsState) -> str:
    if state["qa_passed"] and state["security_passed"]: return "ready"
    return "waiting"

def route_deployment_health(state: DevOpsState) -> str:
    if state["health_check_passed"]:                return "healthy"
    return "unhealthy"

# ── W09 routers ───────────────────────────────────────────────

def route_anomaly_severity(state: MonitoringState) -> str:
    return state.get("severity", "none")

# ── W11 routers ───────────────────────────────────────────────

def route_budget_check(state: CostProtectionState) -> str:
    if state["budget_limit_usd"] is None:           return "unlimited"
    ratio = state["project_total_spend"] / state["budget_limit_usd"]
    if ratio >= 1.0:                                return "exceeded"
    if ratio >= 0.8:                                return "warning"
    return "under_budget"

# ── W12 routers ───────────────────────────────────────────────

def route_task_validation(state: TaskDelegationState) -> str:
    if state["validation_passed"]:                  return "complete"
    if state["retry_count"] >= state["max_retries"]:return "dead_letter"
    return "retry"

def route_escalation(state: TaskDelegationState) -> str:
    if state["escalation_level"] >= 4:              return "dead_letter"
    return "escalate"


# ═══════════════════════════════════════════════════════════════
# SECTION 3 — NODE STUBS
# Signatures + contracts. No implementations.
# Implementations live in the Agent Runtime layer (next doc).
# Every node must:
#   1. Append to nats_events_queue (not publish directly)
#   2. Append to websocket_events_queue for real-time UI updates
#   3. Write token usage to CostProtectionState via trigger
#   4. Return a partial state dict (only modified fields)
# ═══════════════════════════════════════════════════════════════

# ── W01 nodes ─────────────────────────────────────────────────

def project_intake_node(state: ProjectState) -> dict:
    """
    Creates DB records. Initializes workspace. Validates project description.
    Triggers: REST POST /api/v1/projects
    Writes: workflow_id, active_tasks=[], artifacts={}, revision_round=0
    NATS: manager.project.created
    WS  : { type: "project_created", project_id }
    """
    ...

def requirements_phase_node(state: ProjectState) -> dict:
    """
    Publishes manager.task.assigned { department: "product" } to NATS.
    Subscribes to product.requirements.completed.
    Blocks until product service completes or fails.
    Writes: current_phase=2, phase_status="running"
    NATS: manager.task.assigned, manager.project.phase_changed
    """
    ...

def requirements_approval_gate_node(state: ProjectState) -> dict:
    """
    INTERRUPT NODE. Graph pauses here (interrupt_before).
    Sends approval request to user via WebSocket.
    State is frozen until manager-service calls graph.update_state().
    Writes: awaiting_approval=True, approval_artifact_type="requirements"
    WS  : { type: "approval_required", artifact_type: "requirements" }
    NATS: manager.approval.requested
    """
    ...

def requirements_revision_node(state: ProjectState) -> dict:
    """
    Routes rejection feedback back to product-service.
    Increments revision_round. Resets approval_status.
    Publishes NATS event with revision_feedback attached.
    Writes: revision_round+=1, approval_status=None
    NATS: manager.approval.rejected { revision_feedback }
    """
    ...

def validation_phase_node(state: ProjectState) -> dict:
    """
    Fans out to QA, Security, and Docs sub-graphs in parallel via Send().
    Uses LangGraph Send() — all three run simultaneously.
    Waits for all three to complete before routing.
    QA and Security results gate deployment; Docs is non-blocking.
    """
    ...

def budget_exceeded_handler_node(state: ProjectState) -> dict:
    """
    Pauses all active tasks.
    Notifies user via WebSocket with current spend breakdown.
    Marks project paused. Awaits user decision (increase / stop).
    NATS: manager.budget.exceeded
    WS  : { type: "budget_exceeded", spend, limit, breakdown_by_dept }
    """
    ...

def handle_failure_node(state: ProjectState) -> dict:
    """
    Terminal failure handler. Logs to audit_events.
    Notifies user. Preserves full state for post-mortem.
    NATS: manager.project.failed
    WS  : { type: "project_failed", failure_reason }
    """
    ...

def project_complete_node(state: ProjectState) -> dict:
    """
    Marks project completed. Creates final project_version snapshot.
    Generates completion report.
    NATS: manager.project.completed
    WS  : { type: "project_completed", version, artifacts }
    """
    ...

# ── W02 nodes ─────────────────────────────────────────────────

def feature_extraction_node(state: ProductState) -> dict:
    """
    Agent: Feature Analyst Worker (L5)
    Input: project_description
    Output: features list (name, description, priority using MoSCoW)
    LLM call → triggers W11 (Cost Protection).
    """
    ...

def requirements_generation_node(state: ProductState) -> dict:
    """
    Agent: Requirements Writer Worker (L5)
    Input: features list (+ revision_feedback if revision_round > 0)
    Output: requirements list (title, description, category, priority)
    Revision feedback is injected into system prompt on retry rounds.
    """
    ...

def requirements_validation_node(state: ProductState) -> dict:
    """
    Agent: Requirements Validator Worker (L5)
    Checks: completeness, ambiguity, contradictions.
    Output: review_passed bool + validation_notes
    """
    ...

def user_story_generation_node(state: ProductState) -> dict:
    """
    Agent: User Story Writer Worker (L5)
    Format: "As a [role], I want [action], so that [benefit]."
    Output: user_stories list linked to features.
    """
    ...

def acceptance_criteria_node(state: ProductState) -> dict:
    """
    Agent: Acceptance Criteria Worker (L5)
    Format: Given [context] / When [action] / Then [outcome].
    Output: acceptance_criteria list linked to user_stories.
    """
    ...

def product_lead_review_node(state: ProductState) -> dict:
    """
    Agent: Requirements Lead (L4)
    Runs the generate → critique → improve → validate cycle.
    Reviews requirements + user_stories + acceptance_criteria together.
    Sets review_passed = True only if all three pass.
    """
    ...

def product_artifacts_creation_node(state: ProductState) -> dict:
    """
    Writes all artifacts to PostgreSQL.
    Registers each artifact in the artifacts table (status: under_review).
    Writes: artifacts dict with all four artifact IDs.
    """
    ...

def product_await_approval_node(state: ProductState) -> dict:
    """
    INTERRUPT NODE. Pauses here until user approves or rejects.
    Manager-service resumes via graph.update_state() + graph.invoke().
    """
    ...

def product_publish_artifacts_node(state: ProductState) -> dict:
    """
    Sets artifact status = 'approved' in DB.
    Updates requirement_dependencies table (graph links).
    NATS: product.requirements.completed
    """
    ...

def product_revision_handler_node(state: ProductState) -> dict:
    """
    Processes rejection feedback.
    Identifies which artifacts to re-generate (targeted, not full).
    Prepares revision_feedback for injection into next agent context.
    Writes: revision_round+=1, revision_feedback
    """
    ...

# ── W03 nodes ─────────────────────────────────────────────────

def system_design_node(state: ArchitectureState) -> dict:
    """Agent: System Architect Worker (L5). Generates Mermaid diagram + components JSON."""
    ...

def api_design_node(state: ArchitectureState) -> dict:
    """Agent: OpenAPI Spec Writer Worker (L5). Produces full OpenAPI 3.1 spec."""
    ...

def api_review_node(state: ArchitectureState) -> dict:
    """Agent: API Reviewer Worker (L5). Validates spec completeness + REST best practices."""
    ...

def database_design_node(state: ArchitectureState) -> dict:
    """Agent: Schema Designer Worker (L5). Produces DB schema with relationships."""
    ...

def index_optimization_node(state: ArchitectureState) -> dict:
    """Agent: Index Optimizer Worker (L5). Validates schema for query performance."""
    ...

def infrastructure_planning_node(state: ArchitectureState) -> dict:
    """Agent: Infrastructure Planner Worker (L5). Services, ports, volumes, env vars."""
    ...

def traceability_check_node(state: ArchitectureState) -> dict:
    """
    Agent: Architecture Head (L3).
    Verifies every requirement maps to at least one API endpoint or component.
    Writes: traceability_check_passed bool.
    """
    ...

# ── W04 nodes ─────────────────────────────────────────────────

def task_breakdown_node(state: EngineeringState) -> dict:
    """
    Agent: Engineering Head (L3).
    Decomposes architecture into ordered engineering tasks.
    Respects dependency order: models before endpoints, endpoints before pages.
    Writes: task_plan list.
    """
    ...

def fan_out_to_engineering_teams_node(state: EngineeringState) -> List[Send]:
    """
    Fan-out using Send(). Backend, Frontend, Integration run in parallel.
    Each Send passes a slice of EngineeringState + its team's task_plan subset.
    """
    backend_tasks    = [t for t in state["task_plan"] if t["layer"] == "backend"]
    frontend_tasks   = [t for t in state["task_plan"] if t["layer"] == "frontend"]
    integration_tasks= [t for t in state["task_plan"] if t["layer"] == "integration"]
    sends = []
    for task in backend_tasks:
        sends.append(Send("backend_module_subgraph", {**state, "current_task": task}))
    for task in frontend_tasks:
        sends.append(Send("frontend_module_subgraph", {**state, "current_task": task}))
    for task in integration_tasks:
        sends.append(Send("integration_module_subgraph", {**state, "current_task": task}))
    return sends

def code_generation_node(state: Dict[str, Any]) -> dict:
    """
    Agent: Appropriate worker (L5) based on task.layer.
    Generates code for a single module.
    Writes: module code to repository (in-memory first, committed later).
    """
    ...

def code_review_node(state: Dict[str, Any]) -> dict:
    """
    Agent: Code Reviewer Worker (L5).
    Checks: architecture compliance, naming conventions, security basics.
    Writes: review_passed bool + review_notes.
    """
    ...

def refactor_node(state: Dict[str, Any]) -> dict:
    """
    Agent: Refactor Worker (L5).
    Applies Code Reviewer feedback. Re-generates failing sections only.
    """
    ...

def commit_module_node(state: Dict[str, Any]) -> dict:
    """
    Commits approved module code to GitHub via GitHub API.
    Creates commit_records row.
    Registers artifact in artifacts table.
    NATS: engineering.module.committed { module_id, commit_sha }
    """
    ...

# ── W05 nodes ─────────────────────────────────────────────────

def unit_test_generation_node(state: QAState) -> dict:
    """Agent: Unit Test Writer Worker (L5). Tests every function/method."""
    ...

def coverage_analysis_node(state: QAState) -> dict:
    """Agent: Coverage Analyzer Worker (L5). Hard block below 80%."""
    ...

def integration_test_node(state: QAState) -> dict:
    """Agent: Integration Test Writer Worker (L5). 100% of OpenAPI endpoints."""
    ...

def qa_report_node(state: QAState) -> dict:
    """
    Aggregates all test results into a single QA report artifact.
    Sets overall_status = 'passed' only if ALL suites pass.
    NATS: qa.phase.completed OR qa.testing.failed
    """
    ...

# ── W06 nodes ─────────────────────────────────────────────────

def dependency_scan_node(state: SecurityState) -> dict:
    """Agent: CVE Scanner Worker (L5). Checks all deps against CVE databases."""
    ...

def code_security_scan_node(state: SecurityState) -> dict:
    """
    Runs OWASP Checker + Secret Scanner + Injection Check in parallel.
    Each is a sub-call within this node; all three must complete.
    """
    ...

def classify_findings_node(state: SecurityState) -> dict:
    """
    Counts findings by severity.
    Sets blocks_deployment = True if critical_count > 0 or high_count > 0.
    """
    ...

def security_report_node(state: SecurityState) -> dict:
    """
    Creates security_report + vulnerability_list + compliance_checklist artifacts.
    NATS: security.phase.completed OR security.finding.critical
    """
    ...

# ── W08 nodes ─────────────────────────────────────────────────

def await_prerequisites_node(state: DevOpsState) -> dict:
    """
    Blocks until both qa_passed=True AND security_passed=True.
    Reads both flags from DB (set by QA and Security graphs via NATS).
    This node is idempotent — safe to re-enter.
    """
    ...

def generate_dockerfiles_node(state: DevOpsState) -> dict:
    """Agent: Dockerfile Writer Worker (L5). One Dockerfile per service."""
    ...

def generate_docker_compose_node(state: DevOpsState) -> dict:
    """Agent: Docker Compose Worker (L5). Full docker-compose.yml."""
    ...

def generate_cicd_node(state: DevOpsState) -> dict:
    """Agent: Pipeline Config Worker (L5). GitHub Actions workflows."""
    ...

def deployment_plan_node(state: DevOpsState) -> dict:
    """
    Agent: DevOps Head (L3).
    Assembles full deployment plan from all generated configs.
    Registers as artifact. Submits to manager for approval gate.
    NATS: devops.plan.ready
    """
    ...

def devops_await_approval_node(state: DevOpsState) -> dict:
    """INTERRUPT NODE. Pauses until user approves deployment plan."""
    ...

def execute_deployment_node(state: DevOpsState) -> dict:
    """
    Agent: Provisioner Worker (L5).
    Runs docker-compose up (or cloud deploy command).
    Sets deployment_status = 'running'.
    NATS: devops.deployment.started
    """
    ...

def health_check_node(state: DevOpsState) -> dict:
    """
    Agent: Health Check Worker (L5).
    Polls all service /health endpoints.
    Writes: health_check_passed bool + services_checked + failed_services.
    NATS: devops.health.check_passed OR devops.health.check_failed
    """
    ...

def rollback_node(state: DevOpsState) -> dict:
    """
    Triggers automatic rollback to previous successful deployment.
    Logs to audit_events. Notifies user.
    NATS: devops.rollback.completed
    WS  : { type: "deployment_failed_rollback_triggered" }
    """
    ...

# ── W11 nodes ─────────────────────────────────────────────────

def record_token_usage_node(state: CostProtectionState) -> dict:
    """Writes a row to token_ledger. Always runs. Never fails silently."""
    ...

def calculate_project_spend_node(state: CostProtectionState) -> dict:
    """SUM(cost_usd) FROM token_ledger WHERE project_id = X."""
    ...

def budget_warning_node(state: CostProtectionState) -> dict:
    """Non-blocking. Notifies user. Does not pause workflow."""
    ...

def budget_exceeded_node(state: CostProtectionState) -> dict:
    """
    Publishes manager.budget.exceeded.
    Manager-service receives this and pauses the lifecycle graph.
    """
    ...

# ── W12 nodes ─────────────────────────────────────────────────

def select_department_node(state: TaskDelegationState) -> dict:
    """
    Manager Agent routing logic.
    Maps task_type → department. Updates state["department"].
    E.g.: "generate_requirements" → "product"
          "write_api_endpoint"    → "engineering"
          "run_unit_tests"        → "qa"
    """
    ...

def select_agent_node(state: TaskDelegationState) -> dict:
    """
    Department Head selects the most appropriate worker agent.
    Considers: agent specialization, current load, retry_count.
    On retry: may select a different agent than the one that failed.
    Writes: selected_agent (agent_id string).
    """
    ...

def select_model_node(state: TaskDelegationState) -> dict:
    """
    Selects LLM provider and model based on:
    - task complexity (simple → claude-haiku, complex → claude-opus)
    - remaining project budget
    - agent's default_provider / default_model
    - escalation_level (higher level → stronger model)
    Writes: selected_provider, selected_model.
    """
    ...

def assign_task_node(state: TaskDelegationState) -> dict:
    """
    Creates agent_runs row in DB.
    Publishes NATS task assignment event.
    Writes: agent_run_id, task_status="assigned".
    NATS: manager.task.assigned { task_id, agent_id, provider, model }
    """
    ...

def collect_results_node(state: TaskDelegationState) -> dict:
    """
    Waits for agent completion signal (NATS event or polling).
    Reads agent output from agent_runs.output_data.
    Writes: task_output, task_status="completed" or "failed".
    """
    ...

def validate_completion_node(state: TaskDelegationState) -> dict:
    """
    Validates agent output against expected schema and quality criteria.
    Sets validation_passed = True only if output meets all criteria.
    """
    ...

def handle_retry_node(state: TaskDelegationState) -> dict:
    """
    Increments retry_count. Resets agent_run_id.
    Optionally escalates model on third attempt.
    Returns to select_agent_node.
    """
    ...

def escalate_task_node(state: TaskDelegationState) -> dict:
    """
    Increments escalation_level.
    Level 1: Lead Agent reviews and re-assigns.
    Level 2: Department Head intervenes.
    Level 3: Manager Agent makes recovery decision.
    Level 4: User is asked for input.
    """
    ...

def dead_letter_node(state: TaskDelegationState) -> dict:
    """
    Publishes task to dlq.tasks.dead NATS subject.
    Writes audit_events row with status=dead_lettered.
    Notifies manager and user via WebSocket.
    NATS: dlq.tasks.dead { task_id, failure_reason, retry_count }
    WS  : { type: "task_dead_lettered", task_id, reason }
    """
    ...


# ═══════════════════════════════════════════════════════════════
# SECTION 4 — GRAPH BUILDERS
# Each returns a compiled LangGraph graph.
# Checkpointer (PostgresSaver) is injected — not hardcoded.
# ═══════════════════════════════════════════════════════════════

def build_project_lifecycle_graph(checkpointer: PostgresSaver):
    """
    W01 — Master lifecycle graph.
    3 interrupt nodes: requirements, architecture, deployment approvals.
    Manager Agent is the sole executor.
    """
    g = StateGraph(ProjectState)

    # Nodes
    g.add_node("project_intake",              project_intake_node)
    g.add_node("requirements_phase",           requirements_phase_node)
    g.add_node("requirements_approval_gate",   requirements_approval_gate_node)
    g.add_node("requirements_revision",        requirements_revision_node)
    g.add_node("architecture_phase",           architecture_phase_node)      # stub below
    g.add_node("architecture_approval_gate",   architecture_approval_gate_node)
    g.add_node("architecture_revision",        architecture_revision_node)   # stub below
    g.add_node("project_structure",            project_structure_node)       # stub below
    g.add_node("implementation_phase",         implementation_phase_node)    # stub below
    g.add_node("validation_phase",             validation_phase_node)
    g.add_node("engineering_rework",           engineering_rework_node)      # stub below
    g.add_node("deployment_phase",             deployment_phase_node)        # stub below
    g.add_node("deployment_approval_gate",     deployment_approval_gate_node)# stub below
    g.add_node("execute_deployment",           execute_deployment_phase_node)# stub below
    g.add_node("monitoring_phase",             monitoring_phase_node)        # stub below
    g.add_node("budget_exceeded_handler",      budget_exceeded_handler_node)
    g.add_node("handle_failure",               handle_failure_node)
    g.add_node("project_complete",             project_complete_node)

    # Entry
    g.set_entry_point("project_intake")

    # Linear edges
    g.add_edge("project_intake",            "requirements_phase")
    g.add_edge("requirements_phase",         "requirements_approval_gate")
    g.add_edge("requirements_revision",      "requirements_phase")
    g.add_edge("architecture_revision",      "architecture_phase")
    g.add_edge("project_structure",          "implementation_phase")
    g.add_edge("engineering_rework",         "implementation_phase")
    g.add_edge("monitoring_phase",           "project_complete")
    g.add_edge("project_complete",           END)
    g.add_edge("handle_failure",             END)
    g.add_edge("budget_exceeded_handler",    END)

    # Conditional edges
    g.add_conditional_edges("requirements_approval_gate", route_requirements_approval, {
        "approved":       "architecture_phase",
        "rejected":       "requirements_revision",
        "max_revisions":  "handle_failure",
        "budget_exceeded":"budget_exceeded_handler",
        "pending":        "requirements_approval_gate",
    })
    g.add_conditional_edges("architecture_approval_gate", route_architecture_approval, {
        "approved":       "project_structure",
        "rejected":       "architecture_revision",
        "max_revisions":  "handle_failure",
        "budget_exceeded":"budget_exceeded_handler",
        "pending":        "architecture_approval_gate",
    })
    g.add_conditional_edges("implementation_phase", route_implementation, {
        "complete":       "validation_phase",
        "failed":         "handle_failure",
        "budget_exceeded":"budget_exceeded_handler",
    })
    g.add_conditional_edges("validation_phase", route_validation, {
        "all_passed":     "deployment_phase",
        "qa_failed":      "engineering_rework",
        "security_blocked":"handle_failure",
        "budget_exceeded":"budget_exceeded_handler",
    })
    g.add_conditional_edges("deployment_approval_gate", route_deployment_approval, {
        "approved":       "execute_deployment",
        "rejected":       "deployment_phase",
        "budget_exceeded":"budget_exceeded_handler",
        "pending":        "deployment_approval_gate",
    })
    g.add_conditional_edges("execute_deployment", route_after_deployment, {
        "success":        "monitoring_phase",
        "failed":         "handle_failure",
    })
    g.add_edge("architecture_phase", "architecture_approval_gate")
    g.add_edge("deployment_phase",   "deployment_approval_gate")

    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=[
            "requirements_approval_gate",
            "architecture_approval_gate",
            "deployment_approval_gate",
        ]
    )


def build_product_service_graph(checkpointer: PostgresSaver):
    """
    W02 — Product Service Graph.
    1 interrupt node: requirements approval.
    """
    g = StateGraph(ProductState)

    g.add_node("feature_extraction",        feature_extraction_node)
    g.add_node("requirements_generation",   requirements_generation_node)
    g.add_node("requirements_validation",   requirements_validation_node)
    g.add_node("user_story_generation",     user_story_generation_node)
    g.add_node("acceptance_criteria",       acceptance_criteria_node)
    g.add_node("lead_review",               product_lead_review_node)
    g.add_node("artifacts_creation",        product_artifacts_creation_node)
    g.add_node("await_approval",            product_await_approval_node)
    g.add_node("publish_artifacts",         product_publish_artifacts_node)
    g.add_node("revision_handler",          product_revision_handler_node)
    g.add_node("escalate_to_manager",       escalate_product_to_manager_node)

    g.set_entry_point("feature_extraction")
    g.add_edge("feature_extraction",        "requirements_generation")
    g.add_edge("requirements_generation",   "requirements_validation")
    g.add_edge("requirements_validation",   "user_story_generation")
    g.add_edge("user_story_generation",     "acceptance_criteria")
    g.add_edge("acceptance_criteria",       "lead_review")
    g.add_edge("artifacts_creation",        "await_approval")
    g.add_edge("publish_artifacts",         END)
    g.add_edge("revision_handler",          "requirements_generation")
    g.add_edge("escalate_to_manager",       END)

    g.add_conditional_edges("lead_review", route_product_lead_review, {
        "passed":   "artifacts_creation",
        "failed":   "requirements_generation",
        "escalate": "escalate_to_manager",
    })
    g.add_conditional_edges("await_approval", route_product_approval, {
        "approved":       "publish_artifacts",
        "rejected":       "revision_handler",
        "max_revisions":  "escalate_to_manager",
        "pending":        "await_approval",
    })

    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=["await_approval"]
    )


def build_architecture_service_graph(checkpointer: PostgresSaver):
    """W03 — Architecture Service Graph. 1 interrupt node: architecture approval."""
    g = StateGraph(ArchitectureState)

    g.add_node("load_requirements",         load_requirements_node)
    g.add_node("system_design",             system_design_node)
    g.add_node("component_design",          component_design_node)
    g.add_node("api_design",                api_design_node)
    g.add_node("api_review",                api_review_node)
    g.add_node("database_design",           database_design_node)
    g.add_node("index_optimization",        index_optimization_node)
    g.add_node("infrastructure_planning",   infrastructure_planning_node)
    g.add_node("traceability_check",        traceability_check_node)
    g.add_node("architecture_head_review",  architecture_head_review_node)
    g.add_node("artifacts_creation",        architecture_artifacts_creation_node)
    g.add_node("await_approval",            architecture_await_approval_node)
    g.add_node("publish_artifacts",         architecture_publish_artifacts_node)
    g.add_node("revision_handler",          architecture_revision_handler_node)
    g.add_node("escalate_to_manager",       escalate_architecture_to_manager_node)

    g.set_entry_point("load_requirements")
    g.add_edge("load_requirements",         "system_design")
    g.add_edge("system_design",             "component_design")
    # API and DB design run in sequence (DB schema depends on API contracts)
    g.add_edge("component_design",          "api_design")
    g.add_edge("api_design",                "api_review")
    g.add_edge("api_review",                "database_design")
    g.add_edge("database_design",           "index_optimization")
    g.add_edge("index_optimization",        "infrastructure_planning")
    g.add_edge("infrastructure_planning",   "traceability_check")
    g.add_edge("traceability_check",        "architecture_head_review")
    g.add_edge("artifacts_creation",        "await_approval")
    g.add_edge("publish_artifacts",         END)
    g.add_edge("revision_handler",          "system_design")
    g.add_edge("escalate_to_manager",       END)

    g.add_conditional_edges("architecture_head_review", route_architecture_review, {
        "passed":           "artifacts_creation",
        "failed":           "system_design",
        "traceability_gap": "system_design",
        "escalate":         "escalate_to_manager",
    })
    g.add_conditional_edges("await_approval", route_architecture_approval_gate, {
        "approved":       "publish_artifacts",
        "rejected":       "revision_handler",
        "max_revisions":  "escalate_to_manager",
        "pending":        "await_approval",
    })

    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=["await_approval"]
    )


def build_engineering_graph(checkpointer: PostgresSaver):
    """
    W04 — Engineering Graph. Most complex workflow (4 stages).
    No interrupt nodes — continuous autonomous generation.
    """
    g = StateGraph(EngineeringState)

    g.add_node("load_architecture",         load_architecture_node)
    g.add_node("task_breakdown",            task_breakdown_node)
    g.add_node("initialize_repository",     initialize_repository_node)
    g.add_node("fan_out_to_teams",          fan_out_to_engineering_teams_node)
    # Subgraph nodes (each runs the generate→review→refactor→commit cycle)
    g.add_node("backend_module_subgraph",   backend_module_subgraph_node)
    g.add_node("frontend_module_subgraph",  frontend_module_subgraph_node)
    g.add_node("integration_module_subgraph", integration_module_subgraph_node)
    g.add_node("collect_engineering_results", collect_engineering_results_node)
    g.add_node("engineering_complete",      engineering_complete_node)
    g.add_node("handle_partial_failure",    handle_partial_failure_node)
    g.add_node("escalate_to_manager",       escalate_engineering_to_manager_node)

    g.set_entry_point("load_architecture")
    g.add_edge("load_architecture",         "task_breakdown")
    g.add_edge("task_breakdown",            "initialize_repository")
    g.add_edge("initialize_repository",     "fan_out_to_teams")
    # Fan-out is parallel; LangGraph collects all Send() results
    # before advancing to collect_engineering_results
    g.add_edge("backend_module_subgraph",   "collect_engineering_results")
    g.add_edge("frontend_module_subgraph",  "collect_engineering_results")
    g.add_edge("integration_module_subgraph", "collect_engineering_results")
    g.add_edge("engineering_complete",      END)
    g.add_edge("escalate_to_manager",       END)

    g.add_conditional_edges("collect_engineering_results", route_engineering_completion, {
        "complete":    "engineering_complete",
        "partial":     "handle_partial_failure",
        "all_failed":  "escalate_to_manager",
    })
    g.add_edge("handle_partial_failure",    "fan_out_to_teams")

    return g.compile(checkpointer=checkpointer)


def build_qa_graph(checkpointer: PostgresSaver):
    """W05 — QA Graph. Triggered by engineering.phase.completed."""
    g = StateGraph(QAState)

    g.add_node("load_modules",             load_qa_modules_node)
    g.add_node("unit_test_generation",     unit_test_generation_node)
    g.add_node("unit_test_execution",      unit_test_execution_node)
    g.add_node("coverage_analysis",        coverage_analysis_node)
    g.add_node("integration_test",         integration_test_node)
    g.add_node("regression_test",          regression_test_node)
    g.add_node("performance_test",         performance_test_node)
    g.add_node("qa_report",               qa_report_node)
    g.add_node("publish_qa_passed",        publish_qa_passed_node)
    g.add_node("publish_qa_failed",        publish_qa_failed_node)

    g.set_entry_point("load_modules")
    g.add_edge("load_modules",             "unit_test_generation")
    g.add_edge("unit_test_generation",     "unit_test_execution")
    g.add_edge("unit_test_execution",      "coverage_analysis")
    g.add_edge("coverage_analysis",        "integration_test")
    g.add_edge("integration_test",         "regression_test")
    g.add_edge("regression_test",          "performance_test")
    g.add_edge("performance_test",         "qa_report")
    g.add_edge("publish_qa_passed",        END)
    g.add_edge("publish_qa_failed",        END)

    g.add_conditional_edges("qa_report", route_qa_decision, {
        "passed": "publish_qa_passed",
        "failed": "publish_qa_failed",
    })

    return g.compile(checkpointer=checkpointer)


def build_security_graph(checkpointer: PostgresSaver):
    """W06 — Security Graph. Runs in parallel with QA via Send()."""
    g = StateGraph(SecurityState)

    g.add_node("dependency_scan",          dependency_scan_node)
    g.add_node("code_security_scan",       code_security_scan_node)
    g.add_node("secret_scan",              secret_scan_node)
    g.add_node("compliance_check",         compliance_check_node)
    g.add_node("classify_findings",        classify_findings_node)
    g.add_node("security_report",          security_report_node)
    g.add_node("publish_security_passed",  publish_security_passed_node)
    g.add_node("publish_security_blocked", publish_security_blocked_node)
    g.add_node("publish_security_warning", publish_security_warning_node)

    g.set_entry_point("dependency_scan")
    # dependency_scan and code_security_scan run sequentially
    # (code scan needs dep scan results for context)
    g.add_edge("dependency_scan",          "code_security_scan")
    g.add_edge("code_security_scan",       "secret_scan")
    g.add_edge("secret_scan",              "compliance_check")
    g.add_edge("compliance_check",         "classify_findings")
    g.add_edge("classify_findings",        "security_report")
    g.add_edge("publish_security_passed",  END)
    g.add_edge("publish_security_blocked", END)
    g.add_edge("publish_security_warning", END)

    g.add_conditional_edges("security_report", route_security_decision, {
        "passed":  "publish_security_passed",
        "warning": "publish_security_warning",
        "blocked": "publish_security_blocked",
    })

    return g.compile(checkpointer=checkpointer)


def build_docs_graph(checkpointer: PostgresSaver):
    """
    W07 — Documentation Graph.
    Concurrent with other phases. Failures are warnings only.
    """
    g = StateGraph(DocsState)

    g.add_node("generate_api_docs",        generate_api_docs_node)
    g.add_node("generate_code_comments",   generate_code_comments_node)
    g.add_node("generate_readme",          generate_readme_node)
    g.add_node("generate_user_guide",      generate_user_guide_node)
    g.add_node("generate_changelog",       generate_changelog_node)
    g.add_node("publish_docs_complete",    publish_docs_complete_node)

    g.set_entry_point("generate_api_docs")
    g.add_edge("generate_api_docs",        "generate_code_comments")
    g.add_edge("generate_code_comments",   "generate_readme")
    g.add_edge("generate_readme",          "generate_user_guide")
    g.add_edge("generate_user_guide",      "generate_changelog")
    g.add_edge("generate_changelog",       "publish_docs_complete")
    g.add_edge("publish_docs_complete",    END)

    return g.compile(checkpointer=checkpointer)


def build_devops_graph(checkpointer: PostgresSaver):
    """
    W08 — DevOps Graph.
    Starts only after QA AND Security both pass.
    1 interrupt node: deployment plan approval.
    Auto-rollback on health check failure.
    """
    g = StateGraph(DevOpsState)

    g.add_node("await_prerequisites",      await_prerequisites_node)
    g.add_node("generate_dockerfiles",     generate_dockerfiles_node)
    g.add_node("generate_docker_compose",  generate_docker_compose_node)
    g.add_node("generate_cicd",            generate_cicd_node)
    g.add_node("generate_env_templates",   generate_env_templates_node)
    g.add_node("deployment_plan",          deployment_plan_node)
    g.add_node("await_approval",           devops_await_approval_node)
    g.add_node("execute_deployment",       execute_deployment_node)
    g.add_node("health_check",             health_check_node)
    g.add_node("rollback",                 rollback_node)
    g.add_node("deployment_success",       deployment_success_node)
    g.add_node("deployment_failed",        deployment_failed_node)

    g.set_entry_point("await_prerequisites")
    g.add_edge("generate_dockerfiles",     "generate_docker_compose")
    g.add_edge("generate_docker_compose",  "generate_cicd")
    g.add_edge("generate_cicd",            "generate_env_templates")
    g.add_edge("generate_env_templates",   "deployment_plan")
    g.add_edge("deployment_plan",          "await_approval")
    g.add_edge("execute_deployment",       "health_check")
    g.add_edge("rollback",                 "deployment_failed")
    g.add_edge("deployment_success",       END)
    g.add_edge("deployment_failed",        END)

    g.add_conditional_edges("await_prerequisites", route_prerequisites_check, {
        "ready":   "generate_dockerfiles",
        "waiting": "await_prerequisites",
    })
    g.add_conditional_edges("await_approval", route_deployment_approval, {
        "approved": "execute_deployment",
        "rejected": "deployment_plan",
        "pending":  "await_approval",
    })
    g.add_conditional_edges("health_check", route_deployment_health, {
        "healthy":   "deployment_success",
        "unhealthy": "rollback",
    })

    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=["await_approval"]
    )


def build_monitoring_graph(checkpointer: PostgresSaver):
    """W09 — Monitoring Graph. Always-on loop post-deployment."""
    g = StateGraph(MonitoringState)

    g.add_node("collect_metrics",          collect_metrics_node)
    g.add_node("analyze_anomalies",        analyze_anomalies_node)
    g.add_node("classify_severity",        classify_severity_node)
    g.add_node("create_ticket",            create_ticket_node)
    g.add_node("notify_manager",           notify_manager_monitoring_node)
    g.add_node("trigger_incident",         trigger_incident_node)
    g.add_node("loop_back",                monitoring_loop_back_node)

    g.set_entry_point("collect_metrics")
    g.add_edge("collect_metrics",          "analyze_anomalies")
    g.add_edge("analyze_anomalies",        "classify_severity")
    g.add_edge("create_ticket",            "loop_back")
    g.add_edge("notify_manager",           "loop_back")
    g.add_edge("trigger_incident",         END)   # W10 is invoked
    g.add_edge("loop_back",                "collect_metrics")

    g.add_conditional_edges("classify_severity", route_anomaly_severity, {
        "none":   "loop_back",
        "low":    "create_ticket",
        "medium": "notify_manager",
        "high":   "trigger_incident",
    })

    return g.compile(checkpointer=checkpointer)


def build_incident_response_graph(checkpointer: PostgresSaver):
    """W10 — Incident Response Graph. 1 interrupt node: patch approval."""
    g = StateGraph(IncidentState)

    g.add_node("incident_intake",          incident_intake_node)
    g.add_node("log_collection",           log_collection_node)
    g.add_node("root_cause_analysis",      root_cause_analysis_node)
    g.add_node("fix_proposal",             fix_proposal_node)
    g.add_node("qa_validation",            incident_qa_validation_node)
    g.add_node("await_patch_approval",     await_patch_approval_node)
    g.add_node("deploy_patch",             deploy_patch_node)
    g.add_node("create_version",           incident_create_version_node)
    g.add_node("incident_closed",          incident_closed_node)
    g.add_node("incident_failed",          incident_failed_node)

    g.set_entry_point("incident_intake")
    g.add_edge("incident_intake",          "log_collection")
    g.add_edge("log_collection",           "root_cause_analysis")
    g.add_edge("root_cause_analysis",      "fix_proposal")
    g.add_edge("fix_proposal",             "qa_validation")
    g.add_edge("qa_validation",            "await_patch_approval")
    g.add_edge("deploy_patch",             "create_version")
    g.add_edge("create_version",           "incident_closed")
    g.add_edge("incident_closed",          END)
    g.add_edge("incident_failed",          END)

    g.add_conditional_edges("await_patch_approval", lambda s: s.get("approval_status"), {
        "approved": "deploy_patch",
        "rejected": "fix_proposal",
        "pending":  "await_patch_approval",
        None:       "await_patch_approval",
    })

    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=["await_patch_approval"]
    )


def build_cost_protection_graph(checkpointer: PostgresSaver):
    """
    W11 — Cost Protection Graph.
    Triggered after every LLM call. Runs in microseconds.
    Failure here must never block the calling agent.
    """
    g = StateGraph(CostProtectionState)

    g.add_node("record_token_usage",       record_token_usage_node)
    g.add_node("calculate_project_spend",  calculate_project_spend_node)
    g.add_node("no_action",               lambda s: s)
    g.add_node("budget_warning",           budget_warning_node)
    g.add_node("budget_exceeded",          budget_exceeded_node)

    g.set_entry_point("record_token_usage")
    g.add_edge("record_token_usage",       "calculate_project_spend")
    g.add_edge("no_action",                END)
    g.add_edge("budget_warning",           END)
    g.add_edge("budget_exceeded",          END)

    g.add_conditional_edges("calculate_project_spend", route_budget_check, {
        "unlimited":    "no_action",
        "under_budget": "no_action",
        "warning":      "budget_warning",
        "exceeded":     "budget_exceeded",
    })

    return g.compile(checkpointer=checkpointer)


def build_task_delegation_graph(checkpointer: PostgresSaver):
    """
    W12 — Task Delegation Graph. The brain of the platform.
    Routes every task from creation to validated completion.
    Owns retry logic, model selection, escalation, and dead-lettering.
    No interrupt nodes — fully autonomous.
    """
    g = StateGraph(TaskDelegationState)

    g.add_node("select_department",        select_department_node)
    g.add_node("select_agent",             select_agent_node)
    g.add_node("select_model",             select_model_node)
    g.add_node("assign_task",              assign_task_node)
    g.add_node("monitor_progress",         monitor_progress_node)
    g.add_node("collect_results",          collect_results_node)
    g.add_node("validate_completion",      validate_completion_node)
    g.add_node("handle_retry",             handle_retry_node)
    g.add_node("escalate_task",            escalate_task_node)
    g.add_node("dead_letter",              dead_letter_node)
    g.add_node("task_complete",            task_complete_node)

    g.set_entry_point("select_department")
    g.add_edge("select_department",        "select_agent")
    g.add_edge("select_agent",             "select_model")
    g.add_edge("select_model",             "assign_task")
    g.add_edge("assign_task",              "monitor_progress")
    g.add_edge("monitor_progress",         "collect_results")
    g.add_edge("collect_results",          "validate_completion")
    g.add_edge("handle_retry",             "select_agent")   # re-enter from agent selection
    g.add_edge("escalate_task",            "select_agent")
    g.add_edge("dead_letter",              END)
    g.add_edge("task_complete",            END)

    g.add_conditional_edges("validate_completion", route_task_validation, {
        "complete":    "task_complete",
        "retry":       "handle_retry",
        "dead_letter": "dead_letter",
    })
    g.add_conditional_edges("handle_retry", route_escalation, {
        "escalate":    "escalate_task",
        "dead_letter": "dead_letter",
    })

    return g.compile(checkpointer=checkpointer)


# ═══════════════════════════════════════════════════════════════
# SECTION 5 — CHECKPOINTER + GRAPH REGISTRY
# ═══════════════════════════════════════════════════════════════

def create_checkpointer(database_url: str) -> PostgresSaver:
    """
    Creates a PostgresSaver for durable state persistence.
    All graph checkpoints are stored in the AASC PostgreSQL database.
    Table: langgraph_checkpoints (auto-created by PostgresSaver).
    """
    return PostgresSaver.from_conn_string(database_url)


def build_all_graphs(database_url: str) -> dict:
    """
    Builds and returns all 12 compiled graphs.
    Call once at service startup and cache the results.
    Each graph shares the same checkpointer (same DB, different threads).
    """
    checkpointer = create_checkpointer(database_url)
    return {
        "project_lifecycle":    build_project_lifecycle_graph(checkpointer),
        "product_service":      build_product_service_graph(checkpointer),
        "architecture_service": build_architecture_service_graph(checkpointer),
        "engineering":          build_engineering_graph(checkpointer),
        "qa":                   build_qa_graph(checkpointer),
        "security":             build_security_graph(checkpointer),
        "docs":                 build_docs_graph(checkpointer),
        "devops":               build_devops_graph(checkpointer),
        "monitoring":           build_monitoring_graph(checkpointer),
        "incident_response":    build_incident_response_graph(checkpointer),
        "cost_protection":      build_cost_protection_graph(checkpointer),
        "task_delegation":      build_task_delegation_graph(checkpointer),
    }


# ═══════════════════════════════════════════════════════════════
# SECTION 6 — GRAPH INVOCATION PATTERNS
# These are the four standard patterns used across the platform.
# ═══════════════════════════════════════════════════════════════

def start_graph(graph, initial_state: dict, thread_id: str) -> dict:
    """
    Start a new graph execution. thread_id = project_id in most cases.
    Graph runs until the first interrupt or END.
    """
    config = {"configurable": {"thread_id": thread_id}}
    return graph.invoke(initial_state, config)


def resume_after_approval(graph, thread_id: str, approval_update: dict) -> dict:
    """
    Resume a graph paused at an approval gate.
    Called by manager-service after user approves or rejects.

    approval_update example:
    {
        "approval_status": "approved",   # or "rejected"
        "approval_feedback": "LGTM",
    }
    """
    config = {"configurable": {"thread_id": thread_id}}
    graph.update_state(config, approval_update)
    return graph.invoke(None, config)


def get_graph_state(graph, thread_id: str) -> dict:
    """
    Read current graph state without advancing execution.
    Used by manager-service to serve GET /api/v1/projects/{id} status.
    """
    config = {"configurable": {"thread_id": thread_id}}
    return graph.get_state(config)


def trigger_parallel_validation(project_id: str, graphs: dict) -> None:
    """
    Fans out QA, Security, and Docs graphs simultaneously.
    Called by project_lifecycle_graph from validation_phase_node.
    Each runs independently; results are merged before routing.
    """
    import threading
    base_state = {"project_id": project_id, "workflow_id": project_id}

    qa_thread   = threading.Thread(
        target=start_graph,
        args=(graphs["qa"],       {**base_state, "overall_status": "pending"}, f"{project_id}_qa")
    )
    sec_thread  = threading.Thread(
        target=start_graph,
        args=(graphs["security"], {**base_state, "overall_status": "pending"}, f"{project_id}_sec")
    )
    docs_thread = threading.Thread(
        target=start_graph,
        args=(graphs["docs"],     {**base_state},                               f"{project_id}_docs")
    )
    qa_thread.start()
    sec_thread.start()
    docs_thread.start()
    qa_thread.join()
    sec_thread.join()
    docs_thread.join()


# ═══════════════════════════════════════════════════════════════
# END OF AASC_LangGraph_Workflows_v1.py
# Next: Agent Runtime Specification v1
# ═══════════════════════════════════════════════════════════════

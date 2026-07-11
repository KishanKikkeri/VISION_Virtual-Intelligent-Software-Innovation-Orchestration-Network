# AASC — Service Specifications v1

**Project:** Autonomous AI Software Company  
**Status:** Locked  
**Date:** 2026-06-08  
**Next document:** PostgreSQL Schema Design v1

---

## Preface

This is the authoritative reference for all 8 services in the AASC platform. Every implementation must conform exactly to the contracts defined here. No service may own responsibilities, endpoints, events, or artifacts not listed in this document without a formal spec revision.

---

## System overview

```
User
  ↓  REST / WebSocket
manager-service        ← Sole authority over workflow state
  ↓  NATS JetStream
  ├── product-service          Phase 2  Requirements
  ├── architecture-service     Phase 3  Architecture design
  ├── engineering-service      Phase 5  Implementation
  ├── qa-service               Phase 6  Testing        (parallel)
  ├── security-service         Phase 7  Security scan  (parallel with QA)
  ├── devops-service           Phase 8  Deployment
  └── docs-service             Phases 4–8  Documentation (concurrent, non-blocking)
```

---

## Event naming convention

All NATS subjects follow the pattern: `{department}.{entity}.{action}`

```
product.requirements.completed
architecture.api_spec.generated
engineering.module.committed
qa.coverage.failed
security.finding.critical
devops.deployment.approved
manager.approval.requested
manager.budget.exceeded
```

---

## Standard agent message schema

Every inter-agent message must conform to this schema. Free-form communication is not permitted.

```json
{
  "message_id":   "uuid",
  "task_id":      "uuid",
  "project_id":   "uuid",
  "agent_id":     "string",
  "parent_agent": "string",
  "department":   "string",
  "status":       "pending | running | completed | failed | escalated",
  "artifacts":    [],
  "feedback":     [],
  "retry_count":  0,
  "timestamp":    "ISO-8601"
}
```

---

## Retry and dead letter policy (all services)

```yaml
max_retries: 3
retry_strategy: exponential_backoff
retry_delays: [10s, 30s, 90s]
on_exhaustion: publish to dlq.tasks.dead
```

The manager-service consumes `dlq.tasks.dead` and makes the recovery decision.

---

## 1. manager-service

### Role

The CEO of the AI organization. The single authority over workflow state. No other service may change project state, create releases, approve artifacts, or trigger deployments.

### Agents

```
Manager Agent (L2) — singleton
```

### Responsibilities

1. Accept new project submissions and initialize the workflow state machine
2. Maintain the project state machine — single source of truth for every project phase
3. Assign tasks to department heads via NATS JetStream
4. Detect approval gates and pause the workflow; notify user via WebSocket
5. Resume workflow on user approval; route rejection feedback to the relevant department
6. Enforce budget caps by reading `token_ledger`; pause and notify if limit is reached
7. Consume dead letter queue messages and issue recovery decisions
8. Broadcast all workflow events to active WebSocket connections for real-time UI updates
9. Append every state change to `audit_events` (immutable, append-only)
10. Coordinate parallel department execution (e.g., QA and Security run concurrently)
11. Maintain project version snapshots to enable rollback

### REST endpoints

```
POST   /api/v1/projects                     Create a new project
GET    /api/v1/projects                     List projects (paginated, filterable)
GET    /api/v1/projects/{id}                Project status + current phase + progress
GET    /api/v1/projects/{id}/timeline       Full chronological audit timeline
GET    /api/v1/projects/{id}/budget         Current spend vs. limit with per-dept breakdown
GET    /api/v1/projects/{id}/artifacts      All artifacts for this project
POST   /api/v1/projects/{id}/approve        Grant approval (body: optional feedback)
POST   /api/v1/projects/{id}/reject         Reject (body: required revision_feedback)
POST   /api/v1/projects/{id}/pause          Manually pause execution
POST   /api/v1/projects/{id}/resume         Resume paused execution
POST   /api/v1/projects/{id}/rollback       Rollback to a specified version
WS     /ws/projects/{id}                    Real-time event stream per project
```

### NATS events published

```
manager.project.created
manager.project.phase_changed        payload: { from_phase, to_phase, project_id }
manager.task.assigned                payload: { department, task_id, project_id }
manager.approval.requested           payload: { artifact_type, project_id, artifacts[] }
manager.approval.granted             payload: { artifact_type, project_id }
manager.approval.rejected            payload: { artifact_type, revision_feedback }
manager.budget.warning               payload: { project_id, spend, limit, pct: 80 }
manager.budget.exceeded              payload: { project_id, spend, limit }
manager.project.paused
manager.project.resumed
manager.project.completed
manager.project.failed
manager.dlq.recovery_decided        payload: { task_id, action: reassign|escalate|user_input|mark_failed }
```

### NATS events subscribed

```
*.*.completed          All department phase completion events
*.*.failed             All department failure events
*.approval.required    Explicit approval requests from departments
token_ledger.usage.recorded
dlq.tasks.dead
```

### Artifacts produced

None. The manager orchestrates artifact creation by departments; it does not generate artifacts directly.

### Approval gates

| Gate | Trigger event | Blocks transition to |
|------|--------------|----------------------|
| Requirements approval | `product.requirements.completed` | Phase 3 (Architecture) |
| Architecture approval | `architecture.design.completed` | Phase 4 (Project structure) |
| Deployment approval | `devops.plan.ready` | Phase 8 (Deployment) |
| Major release | Manual user trigger | Version bump |

### Database tables owned

```
projects
workflows
workflow_phases
approvals
budget_limits
project_versions
```

### Failure handling

- Department fails 3× → move to DLQ, pause workflow, notify user via WebSocket
- Budget exceeded → pause all running tasks, await user decision (increase limit / stop)
- WebSocket disconnect → buffer events in memory for 10 minutes; deliver on reconnect

---

## 2. product-service

### Role

Requirements engineering department. Transforms a raw project idea into structured requirements, user stories, and acceptance criteria that are unambiguous and testable.

### Agents

```
Product Head (L3)
  ├── Requirements Lead (L4)
  │     ├── Feature Analyst Worker (L5)
  │     └── Requirements Validator Worker (L5)
  ├── User Story Lead (L4)
  │     ├── User Story Writer Worker (L5)
  │     └── Acceptance Criteria Worker (L5)
  └── Product Reviewer (L4)
        └── Requirements Reviewer Worker (L5)
```

### Responsibilities

1. Receive project description from manager-service via NATS
2. Feature Analyst extracts and prioritizes features from the raw idea (MoSCoW method)
3. Requirements Validator checks for completeness, ambiguity, and contradictions
4. User Story Writer creates stories in the format: "As a [role], I want [action], so that [benefit]"
5. Acceptance Criteria Worker writes testable acceptance criteria per story (Given/When/Then)
6. Product Reviewer runs the full review cycle: generate → critique → improve → validate
7. Ensure every requirement is traceable to at least one user story
8. Submit finalized artifacts to manager-service (which raises the approval gate)
9. On rejection: re-run pipeline with user's feedback injected into agent context
10. Version all artifacts on every revision

### REST endpoints

```
GET    /api/v1/product/requirements/{project_id}
GET    /api/v1/product/user-stories/{project_id}
GET    /api/v1/product/features/{project_id}
GET    /api/v1/product/requirements/{project_id}/versions
POST   /api/v1/product/requirements/{project_id}/revise   Trigger revision with feedback
```

### NATS events published

```
product.analysis.started
product.features.extracted
product.requirements.draft_ready
product.requirements.review_completed
product.user_stories.completed
product.requirements.completed           Triggers manager approval gate
product.requirements.revised
```

### NATS events subscribed

```
manager.task.assigned                    Filter: department=product
manager.approval.rejected                Filter: artifact_type=requirements
```

### Artifacts produced

| artifact_type | Description | Versioned |
|---|---|---|
| `requirements_doc` | Structured requirements document | Yes |
| `user_stories_doc` | User stories with acceptance criteria | Yes |
| `feature_spec_doc` | Prioritized feature breakdown | Yes |
| `acceptance_criteria` | Testable acceptance criteria per story | Yes |

### Approval rules

- All four artifacts must be present and internally validated before raising approval
- On rejection: full revision cycle; feedback passed to all relevant sub-agents
- Maximum 5 revision rounds before escalating to manager for human manual input

### Database tables owned

```
requirements
features
user_stories
acceptance_criteria
```

### Failure handling

- LLM produces ambiguous or contradictory requirements → retry up to 3× with stricter critique prompt
- 3 retries exhausted → DLQ with failure reason attached; manager decides recovery action
- Infinite revision loop detection: if 5 revision rounds produce no convergence → escalate to manager

---

## 3. architecture-service

### Role

System design department. Converts approved requirements into a complete technical architecture: system design, API contracts (OpenAPI), database schema, and infrastructure plan.

### Agents

```
Architecture Head (L3)
  ├── System Design Lead (L4)
  │     ├── System Architect Worker (L5)
  │     └── Component Designer Worker (L5)
  ├── API Design Lead (L4)
  │     ├── OpenAPI Spec Writer Worker (L5)
  │     └── API Reviewer Worker (L5)
  ├── Database Design Lead (L4)
  │     ├── Schema Designer Worker (L5)
  │     └── Index Optimizer Worker (L5)
  └── Infrastructure Lead (L4)
        └── Infrastructure Planner Worker (L5)
```

### Responsibilities

1. Receive approved requirements from manager-service
2. System Architect generates high-level architecture diagram (structured JSON + Mermaid)
3. Component Designer defines all service boundaries and their interactions
4. OpenAPI Spec Writer generates complete OpenAPI 3.1 specification for all APIs
5. API Reviewer validates the spec for consistency, completeness, and REST best practices
6. Schema Designer designs the full database schema with relationships and constraints
7. Index Optimizer validates the schema for query performance
8. Infrastructure Planner defines services, ports, volumes, environment variables, and resource requirements
9. Architecture Head validates full consistency: every requirement maps to at least one component or API endpoint (traceability check)
10. Submit artifacts to manager-service for user approval
11. On rejection: identify which artifact(s) the feedback targets; re-run only those sub-agents

### REST endpoints

```
GET    /api/v1/architecture/system/{project_id}
GET    /api/v1/architecture/api-spec/{project_id}
GET    /api/v1/architecture/db-schema/{project_id}
GET    /api/v1/architecture/infrastructure/{project_id}
GET    /api/v1/architecture/{project_id}/versions
```

### NATS events published

```
architecture.analysis.started
architecture.system_design.completed
architecture.api_spec.generated
architecture.db_schema.completed
architecture.infrastructure.planned
architecture.review.completed
architecture.design.completed           Triggers manager approval gate
architecture.design.revised
```

### NATS events subscribed

```
manager.task.assigned                   Filter: department=architecture
manager.approval.rejected               Filter: artifact_type=architecture
product.requirements.completed          Consumed for traceability validation
```

### Artifacts produced

| artifact_type | Description | Versioned |
|---|---|---|
| `system_architecture_doc` | System architecture document + diagram | Yes |
| `openapi_spec` | OpenAPI 3.1 specification | Yes |
| `db_schema_doc` | Full database schema with relationships | Yes |
| `infrastructure_plan` | Services, ports, volumes, environment | Yes |

### Approval rules

- All four artifacts required before approval request is raised
- Traceability check: every requirement must map to at least one component or API endpoint
- On rejection: targeted re-run of affected artifacts only (not full re-generation)

### Database tables owned

```
architecture_designs
api_specifications
db_schemas
infrastructure_plans
```

### Failure handling

- Schema circular dependency → triggers Index Optimizer review loop before proceeding
- Invalid OpenAPI spec (fails validation) → retry up to 3× with targeted correction prompt
- Requirement traceability gaps → Architecture Head re-runs System Architect with gap list

---

## 4. engineering-service

### Role

Software development department. Generates all application code based on approved architecture. Every module goes through the mandatory cycle: generate → review → critique → improve → validate. No module bypasses this cycle.

### Agents

```
Engineering Head (L3)
  ├── Backend Lead (L4)
  │     ├── API Implementation Worker (L5)        REST endpoint handlers
  │     ├── Database Layer Worker (L5)             ORM models, migrations
  │     ├── Authentication Worker (L5)             Auth logic, middleware, guards
  │     └── Business Logic Worker (L5)             Core domain logic
  ├── Frontend Lead (L4)
  │     ├── Component Worker (L5)                  Reusable UI components
  │     ├── Page Worker (L5)                       Full page and route generation
  │     └── State Management Worker (L5)           Store, context, hooks
  ├── Integration Lead (L4)
  │     ├── Third-party Integration Worker (L5)
  │     └── Internal Service Integration Worker (L5)
  └── Code Review Lead (L4)
        ├── Code Reviewer Worker (L5)              Standards + architecture compliance
        └── Refactor Worker (L5)                   Applies reviewer feedback
```

### Responsibilities

1. Receive approved architecture from manager-service
2. Initialize GitHub repository with the folder structure defined in the architecture
3. Engineering Head decomposes architecture into implementation tasks ordered by dependency
4. Assign tasks to Backend, Frontend, and Integration Leads in correct dependency order
5. Every generated module goes through: generate → Code Reviewer Worker → Refactor Worker → validate
6. Code Reviewer validates: architecture compliance, coding standards, security basics, naming
7. Commit approved code to GitHub with structured commit messages
8. Register all committed code as artifacts in the artifact registry
9. Block on architecture ambiguity: Engineering Head escalates to manager, never makes assumptions
10. No module is marked `completed` until it passes Code Reviewer Worker

### REST endpoints

```
GET    /api/v1/engineering/status/{project_id}
GET    /api/v1/engineering/modules/{project_id}
GET    /api/v1/engineering/tasks/{project_id}
GET    /api/v1/engineering/commits/{project_id}
POST   /api/v1/engineering/module/{module_id}/retry   Manually trigger retry
```

### NATS events published

```
engineering.repository.initialized
engineering.task.assigned               payload: { module_id, worker_agent_id }
engineering.module.generating
engineering.module.review_started
engineering.module.review_passed
engineering.module.review_failed        Triggers refactor + retry cycle
engineering.module.committed            Code committed to GitHub
engineering.phase.completed
```

### NATS events subscribed

```
manager.task.assigned                   Filter: department=engineering
architecture.design.completed           Trigger for repository initialization
```

### Artifacts produced

| artifact_type | Description | Versioned |
|---|---|---|
| `source_code` | All application code (stored in GitHub) | Via Git commits |
| `repo_structure` | Repository structure definition file | Yes |
| `commit_log` | Structured commit history record | Append-only |

### Approval rules

- No user approval gates for individual modules (continuous generation)
- Engineering Head may request user input on genuine architectural ambiguity
- Major milestones (e.g., "all backend modules complete") reported to manager as phase checkpoints

### Database tables owned

```
engineering_tasks
code_modules
repository_state
commit_records
```

### Failure handling

- Code review failure → refactor cycle (up to 3× before DLQ escalation)
- GitHub API failure → exponential backoff retry; escalate to manager after 3× failure
- Architecture conflict during implementation → Engineering Head escalates to manager immediately; workflow paused pending Architecture Head clarification

---

## 5. qa-service

### Role

Quality assurance department. Validates every module through unit, integration, regression, and performance testing. Blocks the pipeline on any failure. No path to deployment exists without `qa.phase.completed`.

### Agents

```
QA Head (L3)
  ├── Unit Test Lead (L4)
  │     ├── Unit Test Writer Worker (L5)
  │     └── Coverage Analyzer Worker (L5)
  ├── Integration Test Lead (L4)
  │     └── Integration Test Writer Worker (L5)
  ├── Regression Test Lead (L4)
  │     └── Regression Suite Worker (L5)
  └── Performance Test Lead (L4)
        └── Performance Test Worker (L5)
```

### Responsibilities

1. Receive completed modules from engineering-service via NATS
2. Unit Test Writer generates unit tests for every function and method
3. Coverage Analyzer validates that coverage thresholds are met
4. Integration Test Writer generates tests for all API contracts against the OpenAPI spec
5. Regression Suite Worker runs the full existing test suite to detect regressions
6. Performance Test Worker runs load tests on all critical paths (auth, data writes, core APIs)
7. Generate a comprehensive QA report per module and per phase
8. Return failing modules to engineering-service with full failure details attached
9. Block deployment if any test suite has failures when `engineering.phase.completed` is received

### Coverage thresholds (hard blocks)

```
Unit test coverage:           80% minimum — hard block below this value
Integration test coverage:    100% of endpoints defined in the OpenAPI spec
Critical path coverage:       100% — auth flows, payment flows, data write paths
Performance baseline:         p95 response time < 500ms for all endpoints
```

### REST endpoints

```
GET    /api/v1/qa/report/{project_id}
GET    /api/v1/qa/coverage/{project_id}
GET    /api/v1/qa/module/{module_id}/results
GET    /api/v1/qa/regression/{project_id}/latest
```

### NATS events published

```
qa.testing.started
qa.unit_tests.completed
qa.integration_tests.completed
qa.coverage.passed
qa.coverage.failed                  Returns module to engineering with full report
qa.regression.passed
qa.regression.failed                Blocks pipeline; escalates to manager
qa.performance.passed
qa.performance.failed
qa.testing.passed                   Module-level pass
qa.testing.failed                   Module returned to engineering
qa.phase.completed                  All tests pass; pipeline may proceed to security
```

### NATS events subscribed

```
engineering.module.committed
engineering.phase.completed
```

### Artifacts produced

| artifact_type | Description | Versioned |
|---|---|---|
| `unit_test_suite` | Generated unit test files | Yes |
| `integration_test_suite` | API integration test suite | Yes |
| `qa_report` | QA report per phase | Yes |
| `coverage_report` | Coverage breakdown per module | Yes |

### Approval rules

- No user approval required for module-level tests
- If regression suite fails on a major version boundary: escalate to manager for user decision (fix vs. rollback)

### Database tables owned

```
test_suites
test_results
coverage_records
```

### Failure handling

- Test failure → module returned to engineering with failing tests + stack trace attached
- Engineering gets 3 attempts to fix before DLQ escalation
- Persistent regression failure (>3 rounds) → immediate escalation to manager; user is notified via WebSocket

---

## 6. security-service

### Role

Security validation department. Scans all code and dependencies for vulnerabilities before any deployment is permitted. Cannot be bypassed. No path to deployment without `security.phase.completed`.

### Agents

```
Security Head (L3)
  ├── Dependency Scan Lead (L4)
  │     └── CVE Scanner Worker (L5)
  ├── Code Security Lead (L4)
  │     ├── OWASP Checker Worker (L5)
  │     ├── Secret Scanner Worker (L5)
  │     └── Injection Check Worker (L5)
  └── Compliance Lead (L4)
        └── Compliance Validator Worker (L5)
```

### Responsibilities

1. Scan all dependencies against known CVE databases
2. Check code against OWASP Top 10 vulnerability patterns
3. Scan for accidentally committed secrets (API keys, passwords, tokens, certificates)
4. Validate authentication and authorization implementations
5. Check for injection vulnerabilities: SQL, command injection, XSS, path traversal
6. Classify all findings by severity: Critical, High, Medium, Low, Info
7. Block deployment immediately on any Critical or High finding
8. Require user acknowledgment for Medium findings before proceeding
9. Generate full security report and store in artifact registry
10. Log every scan result to `audit_events`, including clean scans

### Severity and escalation rules

| Severity | Action | Blocks deployment |
|---|---|---|
| Critical | Immediate escalation to Manager + User. Fix required before any progress. | Yes — hard block |
| High | Escalate to Manager. Fix required before proceeding. | Yes — hard block |
| Medium | Warn user via WebSocket. Require explicit acknowledgment. | No |
| Low / Info | Log only. No action required. | No |

### REST endpoints

```
GET    /api/v1/security/report/{project_id}
GET    /api/v1/security/findings/{project_id}
GET    /api/v1/security/findings/{project_id}?severity=critical
POST   /api/v1/security/acknowledge/{finding_id}   User acknowledges a medium finding
```

### NATS events published

```
security.scan.started
security.dependencies.scanned
security.code.scanned
security.finding.critical               Immediate block + escalation
security.finding.high                   Block + escalation
security.finding.medium                 Warn; requires acknowledgment
security.scan.passed
security.scan.failed
security.phase.completed
```

### NATS events subscribed

```
qa.phase.completed                      Trigger: begin security scan after QA passes
engineering.code.committed              Optional: lightweight incremental scan
```

### Artifacts produced

| artifact_type | Description | Versioned |
|---|---|---|
| `security_report` | Full security scan report | Yes |
| `vulnerability_list` | All findings with severity and remediation | Yes |
| `compliance_checklist` | Compliance validation results | Yes |

### Approval rules

- Critical/High findings require a code fix and re-scan before proceeding — acknowledgment is not sufficient
- The manager immediately notifies the user via WebSocket on any Critical finding
- `security.phase.completed` is only published when zero Critical and zero High findings remain

### Database tables owned

```
security_scans
security_findings
compliance_records
```

### Failure handling

- Scan tooling failure (network error, API unavailable) → retry 3×, then escalate to manager with full error context
- Security Head cannot be bypassed under any condition; if security-service is unavailable, the pipeline holds

---

## 7. devops-service

### Role

Infrastructure and deployment department. Generates all deployment artifacts, prepares the deployment plan, executes deployments on user approval, and validates post-deployment health.

### Agents

```
DevOps Head (L3)
  ├── Container Lead (L4)
  │     ├── Dockerfile Writer Worker (L5)
  │     └── Docker Compose Worker (L5)
  ├── CI/CD Lead (L4)
  │     ├── Pipeline Config Worker (L5)           GitHub Actions workflows
  │     └── Environment Config Worker (L5)        .env templates, config documentation
  └── Infrastructure Lead (L4)
        ├── Provisioner Worker (L5)               Cloud/VPS resource setup
        └── Health Check Worker (L5)              Post-deployment service validation
```

### Responsibilities

1. Receive deployment trigger from manager-service after QA and Security both pass
2. Dockerfile Writer generates optimized, minimal Dockerfiles for each service
3. Docker Compose Worker generates `docker-compose.yml` with all services, volumes, and networks
4. Pipeline Config Worker generates CI/CD pipeline configuration (GitHub Actions)
5. Environment Config Worker generates `.env.example` templates listing all required variables with descriptions
6. Prepare complete, human-readable deployment plan
7. Submit deployment plan to manager-service (raises approval gate)
8. Execute deployment only after explicit user approval — no exceptions
9. Health Check Worker validates all services respond correctly post-deployment
10. On health check failure: trigger automatic rollback; notify user via WebSocket

### REST endpoints

```
GET    /api/v1/devops/plan/{project_id}         Deployment plan document
GET    /api/v1/devops/status/{project_id}        Current deployment status
GET    /api/v1/devops/history/{project_id}       All deployments for this project
POST   /api/v1/devops/deploy/{project_id}        Trigger deployment (requires prior approval)
GET    /api/v1/devops/health/{project_id}        Post-deployment health status
```

### NATS events published

```
devops.artifacts.generating
devops.dockerfile.completed
devops.compose.completed
devops.cicd.completed
devops.plan.ready                       Triggers manager approval gate
devops.deployment.started
devops.deployment.completed
devops.deployment.failed
devops.health.check_passed
devops.health.check_failed              Auto-rollback triggered; user notified
devops.rollback.completed
devops.phase.completed
```

### NATS events subscribed

```
security.phase.completed                Trigger: begin devops after security passes
qa.phase.completed                      Confirm QA also passed
manager.approval.granted                Filter: artifact_type=deployment_plan
manager.approval.rejected               Filter: artifact_type=deployment_plan
```

### Artifacts produced

| artifact_type | Description | Versioned |
|---|---|---|
| `dockerfile` | Dockerfile per service | Yes |
| `docker_compose_yml` | Complete Docker Compose configuration | Yes |
| `cicd_config` | CI/CD pipeline configuration | Yes |
| `env_template` | Environment variable template (.env.example) | Yes |
| `deployment_plan` | Human-readable deployment plan document | Yes |

### Approval rules

- Deployment plan MUST receive explicit user approval before any deployment begins — hard gate, no exceptions
- Deployment approval is scoped to a specific artifact version; re-generating artifacts requires a new approval

### Database tables owned

```
deployments
deployment_plans
infrastructure_configs
health_checks
```

### Failure handling

- Deployment failure → immediate automatic rollback to last successful deployment
- Health check failure post-deployment → automatic rollback; failure logged to audit_events; user notified
- All rollbacks are logged with full context: what failed, which version was restored, timestamp

---

## 8. docs-service

### Role

Documentation department. Generates and maintains all project documentation concurrently with development. Docs-service does not block any pipeline stage — it runs as a parallel background process subscribed to the same events as other services.

### Agents

```
Docs Head (L3)
  ├── API Docs Lead (L4)
  │     ├── OpenAPI Docs Worker (L5)
  │     └── SDK Docs Worker (L5)
  ├── Code Docs Lead (L4)
  │     ├── Code Comment Worker (L5)
  │     └── README Writer Worker (L5)
  └── User Guide Lead (L4)
        ├── User Guide Writer Worker (L5)
        └── Changelog Writer Worker (L5)
```

### Responsibilities

1. Subscribe to architecture events and begin generating API documentation immediately after approval
2. Subscribe to engineering events and generate inline code docstrings and README files as modules complete
3. Generate user guides based on approved user stories (begin after product approval)
4. Maintain a changelog that updates automatically on every version increment
5. All documentation versioned in the artifact registry
6. Docs Reviewer validates documentation for accuracy, completeness, and consistency with architecture
7. Never hold up any pipeline stage — documentation failures are warnings, not errors

### REST endpoints

```
GET    /api/v1/docs/api/{project_id}
GET    /api/v1/docs/readme/{project_id}
GET    /api/v1/docs/user-guide/{project_id}
GET    /api/v1/docs/changelog/{project_id}
```

### NATS events published

```
docs.api_docs.completed
docs.readme.completed
docs.user_guide.completed
docs.changelog.updated
docs.phase.completed
```

### NATS events subscribed

```
architecture.design.completed           Begin API documentation
engineering.module.committed            Begin code docs for that module
manager.project.phase_changed           Update changelog
product.requirements.completed          Begin user guide drafting
```

### Artifacts produced

| artifact_type | Description | Versioned |
|---|---|---|
| `api_documentation` | Full API documentation from OpenAPI spec | Yes |
| `readme` | Project README | Yes |
| `user_guide` | End-user guide derived from user stories | Yes |
| `changelog` | Version changelog, auto-updated | Append-only |

### Approval rules

- No approval gates for documentation
- User may request documentation revisions at any time without blocking the pipeline

### Database tables owned

```
documentation
changelogs
```

### Failure handling

- Documentation failures do not block the pipeline; reported to manager as warnings
- Failed documentation jobs are retried in the background and can be re-triggered on demand

---

## Cross-service protocols

### Approval gate protocol

```
1. Department finishes phase → publishes *.completed event
2. Manager Agent receives event
3. Manager checks: does this phase have an approval gate?
4. If YES:
   a. Publish manager.approval.requested
   b. Broadcast approval_required over WebSocket to frontend
   c. Pause workflow — no further task assignments
   d. User reviews artifacts in the UI
   e. APPROVE → publish manager.approval.granted → resume workflow
   f. REJECT  → publish manager.approval.rejected (with revision_feedback)
              → route feedback to Department Head
              → department runs revision cycle
              → return to step 1
```

### Dead letter protocol

```
1. Task fails max_retries (3) attempts with exponential backoff
2. NATS JetStream moves task to subject: dlq.tasks.dead
3. Manager Agent receives DLQ message
4. Manager appends to audit_events with status=dead_lettered
5. Manager evaluates recovery options:
   a. reassign_agent   — assign same task to a different worker agent
   b. escalate_model   — retry with a stronger LLM model (e.g., GPT-4o → o1)
   c. request_user_input — surface to user with full context and options
   d. mark_failed      — mark task and project phase as failed; stop execution
6. User is notified via WebSocket regardless of which decision is taken
```

### Budget enforcement protocol

```
1. Every LLM call writes a record to token_ledger table immediately
2. Manager Agent subscribes to token_ledger.usage.recorded
3. On each record: sum total project spend; compare to project budget_limit
4. If spend >= 80% of limit:
   → Publish manager.budget.warning
   → Notify user via WebSocket (non-blocking — execution continues)
5. If spend >= 100% of limit:
   → Publish manager.budget.exceeded
   → Pause all running tasks for this project
   → Notify user via WebSocket
   → Await user decision: increase budget / stop project / continue without limit
```

### Artifact registry protocol

Every generated artifact must be registered before it is considered complete.

```json
{
  "artifact_id":   "uuid",
  "project_id":    "uuid",
  "artifact_type": "requirements_doc | openapi_spec | db_schema_doc | ...",
  "version":       3,
  "created_by":    "agent_id",
  "approved_by":   "user_id | null",
  "status":        "draft | under_review | approved | superseded",
  "storage_ref":   "path or URL to artifact content",
  "created_at":    "ISO-8601"
}
```

Artifact lifecycle:
```
draft → under_review → approved
                     ↘ rejected → (revision) → draft
approved → superseded (when a new version is approved)
```

---

## Artifact type registry

All valid `artifact_type` values across all services:

| Type | Produced by | Requires approval |
|---|---|---|
| `requirements_doc` | product-service | Yes (user) |
| `user_stories_doc` | product-service | Yes (user) |
| `feature_spec_doc` | product-service | Yes (user) |
| `acceptance_criteria` | product-service | Yes (user) |
| `system_architecture_doc` | architecture-service | Yes (user) |
| `openapi_spec` | architecture-service | Yes (user) |
| `db_schema_doc` | architecture-service | Yes (user) |
| `infrastructure_plan` | architecture-service | Yes (user) |
| `source_code` | engineering-service | No |
| `repo_structure` | engineering-service | No |
| `unit_test_suite` | qa-service | No |
| `integration_test_suite` | qa-service | No |
| `qa_report` | qa-service | No |
| `coverage_report` | qa-service | No |
| `security_report` | security-service | No (Critical/High auto-blocks) |
| `vulnerability_list` | security-service | No |
| `compliance_checklist` | security-service | No |
| `dockerfile` | devops-service | No |
| `docker_compose_yml` | devops-service | No |
| `cicd_config` | devops-service | No |
| `env_template` | devops-service | No |
| `deployment_plan` | devops-service | Yes (user) |
| `api_documentation` | docs-service | No |
| `readme` | docs-service | No |
| `user_guide` | docs-service | No |
| `changelog` | docs-service | No |

---

## Service quick reference

| Service | L3 Agent | Approval gate | Artifacts | NATS events pub |
|---|---|---|---|---|
| manager-service | Manager Agent | — (enforces gates) | 0 | 12 |
| product-service | Product Head | Requirements | 4 | 7 |
| architecture-service | Architecture Head | Architecture design | 4 | 7 |
| engineering-service | Engineering Head | None | 3 | 8 |
| qa-service | QA Head | None | 4 | 10 |
| security-service | Security Head | None (auto-block) | 3 | 8 |
| devops-service | DevOps Head | Deployment plan | 5 | 11 |
| docs-service | Docs Head | None | 4 | 5 |

---

*End of AASC Service Specifications v1*  
*Next: PostgreSQL Schema Design v1*

"""
core/runtime/factory.py
========================
AgentFactory  — creates agents by agent_id, injecting all dependencies.
AgentRegistry — central YAML-like registry of all 53 agent definitions.

The entire platform uses:
    agent = AgentFactory.create("requirements_writer_worker")
    result = await agent.run(task)

Never:
    agent = RequirementsWriterWorker()
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Type

import structlog

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════
# AGENT REGISTRY
# ═══════════════════════════════════════════════════════════════

@dataclass
class AgentSpec:
    """Describes one agent in the registry."""
    agent_id:         str
    name:             str
    department:       str
    layer:            int         # 2=manager 3=head 4=lead 5=worker
    role:             str         # manager|head|lead|worker
    parent_agent_id:  Optional[str]
    responsibilities: List[str]
    default_provider: str = "anthropic"
    default_model:    str = "claude-sonnet-4-6"


# Complete registry of all 53 agents
AGENT_REGISTRY: Dict[str, AgentSpec] = {

    # ── manager-service (1 agent) ──────────────────────────────
    "manager_agent": AgentSpec(
        agent_id="manager_agent", name="Manager Agent",
        department="manager", layer=2, role="manager",
        parent_agent_id=None,
        responsibilities=[
            "Sole authority over workflow state transitions",
            "Orchestrate all department heads",
            "Enforce budget limits and approval gates",
            "Handle dead-letter queue recovery decisions",
            "Communicate with the user on approvals and escalations",
        ],
    ),

    # ── product-service (9 agents) ────────────────────────────
    "product_head": AgentSpec(
        agent_id="product_head", name="Product Head",
        department="product", layer=3, role="head",
        parent_agent_id="manager_agent",
        responsibilities=[
            "Run the entire requirements engineering department",
            "Coordinate Requirements Lead, Validation Lead, Artifact Lead",
            "Validate full requirements traceability before submission",
            "Submit completed requirements package to Manager Agent",
        ],
    ),
    "requirements_lead": AgentSpec(
        agent_id="requirements_lead", name="Requirements Lead",
        department="product", layer=4, role="lead",
        parent_agent_id="product_head",
        responsibilities=[
            "Coordinate Feature Analyst, Requirements Writer, and Story Writer workers",
            "Review and validate all requirements artifacts",
            "Ensure MoSCoW prioritization is applied consistently",
        ],
    ),
    "feature_analyst_worker": AgentSpec(
        agent_id="feature_analyst_worker", name="Feature Analyst",
        department="product", layer=5, role="worker",
        parent_agent_id="requirements_lead",
        responsibilities=[
            "Extract and prioritize features from raw project descriptions",
            "Apply MoSCoW method to feature prioritization",
            "Identify implicit requirements and surface them explicitly",
        ],
    ),
    "requirements_writer_worker": AgentSpec(
        agent_id="requirements_writer_worker", name="Requirements Writer",
        department="product", layer=5, role="worker",
        parent_agent_id="requirements_lead",
        responsibilities=[
            "Generate structured requirements from extracted features",
            "Categorize requirements: functional, non-functional, constraint, assumption",
            "Ensure each requirement is unambiguous and testable",
        ],
    ),
    "user_story_writer_worker": AgentSpec(
        agent_id="user_story_writer_worker", name="User Story Writer",
        department="product", layer=5, role="worker",
        parent_agent_id="requirements_lead",
        responsibilities=[
            "Write user stories in the format: As a [role], I want [action], so that [benefit]",
            "Link every user story to one or more requirements",
            "Ensure stories are independent, negotiable, valuable, estimable, small, testable",
        ],
    ),
    "validation_lead": AgentSpec(
        agent_id="validation_lead", name="Validation Lead",
        department="product", layer=4, role="lead",
        parent_agent_id="product_head",
        responsibilities=[
            "Coordinate Acceptance Criteria Worker and Requirements Reviewer",
            "Ensure all user stories have testable acceptance criteria",
            "Validate requirements for completeness and consistency",
        ],
    ),
    "acceptance_criteria_worker": AgentSpec(
        agent_id="acceptance_criteria_worker", name="Acceptance Criteria Writer",
        department="product", layer=5, role="worker",
        parent_agent_id="validation_lead",
        responsibilities=[
            "Write acceptance criteria in Given/When/Then format for every user story",
            "Ensure criteria are binary — clearly pass or fail",
            "Cover both happy path and edge cases",
        ],
    ),
    "requirements_reviewer_worker": AgentSpec(
        agent_id="requirements_reviewer_worker", name="Requirements Reviewer",
        department="product", layer=5, role="worker",
        parent_agent_id="validation_lead",
        responsibilities=[
            "Review all requirements for ambiguity, contradictions, and gaps",
            "Validate that every requirement is traceable to at least one user story",
            "Produce a structured review report with pass/fail per requirement",
        ],
    ),
    "artifact_lead": AgentSpec(
        agent_id="artifact_lead", name="Artifact Lead",
        department="product", layer=4, role="lead",
        parent_agent_id="product_head",
        responsibilities=[
            "Coordinate artifact creation and registry workers",
            "Manage artifact versioning and status transitions",
            "Trigger approval submission to manager-service",
        ],
    ),

    # ── architecture-service (12 agents) ──────────────────────
    "architecture_head": AgentSpec(
        agent_id="architecture_head", name="Architecture Head",
        department="architecture", layer=3, role="head",
        parent_agent_id="manager_agent",
        responsibilities=[
            "Run the entire architecture design department",
            "Validate traceability: every requirement maps to a component or API",
            "Submit architecture blueprint to Manager Agent for approval",
        ],
        default_model="claude-opus-4-6",
    ),
    "system_design_lead": AgentSpec(
        agent_id="system_design_lead", name="System Design Lead",
        department="architecture", layer=4, role="lead",
        parent_agent_id="architecture_head",
        responsibilities=["Coordinate system architect and component designer workers"],
    ),
    "system_architect_worker": AgentSpec(
        agent_id="system_architect_worker", name="System Architect",
        department="architecture", layer=5, role="worker",
        parent_agent_id="system_design_lead",
        responsibilities=["Generate high-level system architecture with Mermaid diagram"],
    ),
    "component_designer_worker": AgentSpec(
        agent_id="component_designer_worker", name="Component Designer",
        department="architecture", layer=5, role="worker",
        parent_agent_id="system_design_lead",
        responsibilities=["Define all service boundaries and their interactions"],
    ),
    "api_design_lead": AgentSpec(
        agent_id="api_design_lead", name="API Design Lead",
        department="architecture", layer=4, role="lead",
        parent_agent_id="architecture_head",
        responsibilities=["Coordinate OpenAPI spec writer and API reviewer"],
    ),
    "openapi_spec_writer_worker": AgentSpec(
        agent_id="openapi_spec_writer_worker", name="OpenAPI Spec Writer",
        department="architecture", layer=5, role="worker",
        parent_agent_id="api_design_lead",
        responsibilities=["Generate complete OpenAPI 3.1 specification for all APIs"],
    ),
    "api_reviewer_worker": AgentSpec(
        agent_id="api_reviewer_worker", name="API Reviewer",
        department="architecture", layer=5, role="worker",
        parent_agent_id="api_design_lead",
        responsibilities=["Validate OpenAPI spec for REST best practices and completeness"],
    ),
    "database_design_lead": AgentSpec(
        agent_id="database_design_lead", name="Database Design Lead",
        department="architecture", layer=4, role="lead",
        parent_agent_id="architecture_head",
        responsibilities=["Coordinate schema designer and index optimizer workers"],
    ),
    "schema_designer_worker": AgentSpec(
        agent_id="schema_designer_worker", name="Schema Designer",
        department="architecture", layer=5, role="worker",
        parent_agent_id="database_design_lead",
        responsibilities=["Design full database schema with relationships and constraints"],
    ),
    "index_optimizer_worker": AgentSpec(
        agent_id="index_optimizer_worker", name="Index Optimizer",
        department="architecture", layer=5, role="worker",
        parent_agent_id="database_design_lead",
        responsibilities=["Validate schema for query performance, add indexes"],
    ),
    "infrastructure_lead": AgentSpec(
        agent_id="infrastructure_lead", name="Infrastructure Lead",
        department="architecture", layer=4, role="lead",
        parent_agent_id="architecture_head",
        responsibilities=["Coordinate infrastructure planning worker"],
    ),
    "infrastructure_planner_worker": AgentSpec(
        agent_id="infrastructure_planner_worker", name="Infrastructure Planner",
        department="architecture", layer=5, role="worker",
        parent_agent_id="infrastructure_lead",
        responsibilities=["Define services, ports, volumes, and resource requirements"],
    ),


    # ── architecture-service additions (M3.1) ────────────────
    "platform_design_lead": AgentSpec(
        agent_id="platform_design_lead", name="Platform Design Lead",
        department="architecture", layer=4, role="lead",
        parent_agent_id="architecture_head",
        responsibilities=[
            "Run 4 platform design workers in parallel via asyncio.gather()",
            "Coordinate: InfrastructureArchitect, SecurityArchitect, ScalabilityArchitect, IntegrationArchitect",
            "Accept partial success — all 4 artifacts are independent",
        ],
    ),
    "architecture_review_lead": AgentSpec(
        agent_id="architecture_review_lead", name="Architecture Review Lead",
        department="architecture", layer=4, role="lead",
        parent_agent_id="architecture_head",
        responsibilities=[
            "Run TraceabilityAgent then ArchitectureReviewer sequentially",
            "Enforce 80% coverage threshold before review proceeds",
            "Escalate to ArchitectureHead on persistent traceability gaps",
        ],
    ),
    "security_architect_worker": AgentSpec(
        agent_id="security_architect_worker", name="Security Architect",
        department="architecture", layer=5, role="worker",
        parent_agent_id="platform_design_lead",
        responsibilities=[
            "Design authentication strategy (JWT + RBAC)",
            "Define OWASP mitigation plan for all Top-10 risks",
            "Specify secrets management and transport security",
        ],
        default_model="claude-opus-4-6",
    ),
    "scalability_architect_worker": AgentSpec(
        agent_id="scalability_architect_worker", name="Scalability Architect",
        department="architecture", layer=5, role="worker",
        parent_agent_id="platform_design_lead",
        responsibilities=[
            "Define horizontal scaling strategy per service",
            "Design Redis caching layer with TTL and invalidation rules",
            "Specify rate limiting and NATS backpressure configuration",
        ],
    ),
    "integration_architect_worker": AgentSpec(
        agent_id="integration_architect_worker", name="Integration Architect",
        department="architecture", layer=5, role="worker",
        parent_agent_id="platform_design_lead",
        responsibilities=[
            "Identify and plan all external third-party integrations",
            "Define provider abstraction pattern for all external services",
            "Specify NATS event contracts for inter-service communication",
        ],
    ),
    "traceability_agent_worker": AgentSpec(
        agent_id="traceability_agent_worker", name="Traceability Agent",
        department="architecture", layer=5, role="worker",
        parent_agent_id="architecture_review_lead",
        responsibilities=[
            "Map every functional requirement through: Feature → API → Service → DB Table",
            "Write requirement→component relationships to requirement_dependencies table",
            "Compute coverage percentage; flag uncovered requirements",
        ],
    ),
    "architecture_reviewer_worker": AgentSpec(
        agent_id="architecture_reviewer_worker", name="Architecture Reviewer",
        department="architecture", layer=5, role="worker",
        parent_agent_id="architecture_review_lead",
        responsibilities=[
            "Cross-cutting consistency check across all 7 architecture artifacts",
            "Verify every blueprint service has a deployment container",
            "Confirm all env vars in deployment are in secrets management",
        ],
    ),

    # ── architecture-service Appendix A addition (M3.3 prerequisite) ──
    "ui_architect_worker": AgentSpec(
        agent_id="ui_architect_worker", name="UI Architect",
        department="architecture", layer=5, role="worker",
        parent_agent_id="platform_design_lead",
        responsibilities=[
            "Produce the ui_blueprint artifact: pages, routes, components, layouts",
            "Define navigation, forms, tables, user flows",
            "Define state boundaries and API bindings for the frontend",
        ],
    ),

    # ── engineering-service (20 agents, M3.3) ─────────────────
    "engineering_head": AgentSpec(
        agent_id="engineering_head", name="Engineering Head",
        department="engineering", layer=3, role="head",
        parent_agent_id="manager_agent",
        responsibilities=[
            "Run the entire Engineering department — decompose architecture into an implementation plan",
            "Coordinate Backend, Frontend, Integration, and Review leads",
            "Enforce that nothing reaches Repository Service before Review completes",
            "Submit merge-ready implementation to Manager Agent",
        ],
        default_model="claude-opus-4-6",
    ),
    "backend_lead": AgentSpec(
        agent_id="backend_lead", name="Backend Lead",
        department="engineering", layer=4, role="lead",
        parent_agent_id="engineering_head",
        responsibilities=["Coordinate Database, Auth, Business Logic, and API workers"],
    ),
    "database_layer_worker": AgentSpec(
        agent_id="database_layer_worker", name="Database Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="backend_lead",
        responsibilities=["Generate ORM models and database migrations from database_schema"],
    ),
    "authentication_worker": AgentSpec(
        agent_id="authentication_worker", name="Auth Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="backend_lead",
        responsibilities=["Implement auth logic, middleware, and guards"],
    ),
    "business_logic_worker": AgentSpec(
        agent_id="business_logic_worker", name="Business Logic Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="backend_lead",
        responsibilities=["Implement core domain business logic"],
    ),
    "api_implementation_worker": AgentSpec(
        agent_id="api_implementation_worker", name="API Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="backend_lead",
        responsibilities=["Generate REST endpoint handlers per openapi_spec"],
    ),
    "frontend_lead": AgentSpec(
        agent_id="frontend_lead", name="Frontend Lead",
        department="engineering", layer=4, role="lead",
        parent_agent_id="engineering_head",
        responsibilities=[
            "Coordinate Component, Page, State, and Routing workers",
            "Refuse to generate UI without an approved ui_blueprint",
        ],
    ),
    "component_worker": AgentSpec(
        agent_id="component_worker", name="Component Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="frontend_lead",
        responsibilities=["Generate reusable UI components from ui_blueprint.components"],
    ),
    "page_worker": AgentSpec(
        agent_id="page_worker", name="Page Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="frontend_lead",
        responsibilities=["Generate full pages from ui_blueprint.pages"],
    ),
    "state_management_worker": AgentSpec(
        agent_id="state_management_worker", name="State Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="frontend_lead",
        responsibilities=["Implement state management from ui_blueprint.state_boundaries"],
    ),
    "routing_worker": AgentSpec(
        agent_id="routing_worker", name="Routing Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="frontend_lead",
        responsibilities=["Generate route definitions and navigation guards from ui_blueprint.routes"],
    ),
    "integration_lead": AgentSpec(
        agent_id="integration_lead", name="Integration Lead",
        department="engineering", layer=4, role="lead",
        parent_agent_id="engineering_head",
        responsibilities=["Coordinate Internal Event, External API, and Messaging workers"],
    ),
    "internal_integration_worker": AgentSpec(
        agent_id="internal_integration_worker", name="Internal Event Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="integration_lead",
        responsibilities=["Implement inter-service event contracts and handlers"],
    ),
    "third_party_integration_worker": AgentSpec(
        agent_id="third_party_integration_worker", name="External API Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="integration_lead",
        responsibilities=["Implement third-party/external service integrations"],
    ),
    "messaging_worker": AgentSpec(
        agent_id="messaging_worker", name="Messaging Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="integration_lead",
        responsibilities=["Implement NATS publishers/subscribers and message contracts"],
    ),
    "code_review_lead": AgentSpec(
        agent_id="code_review_lead", name="Review Lead",
        department="engineering", layer=4, role="lead",
        parent_agent_id="engineering_head",
        responsibilities=[
            "Coordinate Code Review, Refactor, Quality, and Commit workers",
            "Mandatory gate — nothing reaches Repository Service before this team completes",
        ],
    ),
    "code_reviewer_worker": AgentSpec(
        agent_id="code_reviewer_worker", name="Code Review Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="code_review_lead",
        responsibilities=["Review code for architecture compliance and coding standards"],
    ),
    "refactor_worker": AgentSpec(
        agent_id="refactor_worker", name="Refactor Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="code_review_lead",
        responsibilities=["Apply code review feedback and refactor failing sections"],
    ),
    "quality_worker": AgentSpec(
        agent_id="quality_worker", name="Quality Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="code_review_lead",
        responsibilities=[
            "Validate the coding contract: buildable, runnable, testable, traceable, "
            "deterministic, reviewable, idempotent",
        ],
    ),
    "commit_worker": AgentSpec(
        agent_id="commit_worker", name="Commit Worker",
        department="engineering", layer=5, role="worker",
        parent_agent_id="code_review_lead",
        responsibilities=[
            "The only Engineering worker permitted to call Repository Service",
            "Create integration branch, commit files, open pull request",
        ],
    ),

    # ── qa-service (10 agents) ────────────────────────────────
    "qa_head": AgentSpec(
        agent_id="qa_head", name="QA Head",
        department="qa", layer=3, role="head",
        parent_agent_id="manager_agent",
        responsibilities=["Run all testing — unit, integration, regression, performance"],
    ),
    "unit_test_lead": AgentSpec(agent_id="unit_test_lead", name="Unit Test Lead",
        department="qa", layer=4, role="lead", parent_agent_id="qa_head",
        responsibilities=["Coordinate unit test writer and coverage analyzer"]),
    "unit_test_writer_worker": AgentSpec(agent_id="unit_test_writer_worker",
        name="Unit Test Writer", department="qa", layer=5, role="worker",
        parent_agent_id="unit_test_lead",
        responsibilities=["Generate unit tests for every function and method"]),
    "coverage_analyzer_worker": AgentSpec(agent_id="coverage_analyzer_worker",
        name="Coverage Analyzer", department="qa", layer=5, role="worker",
        parent_agent_id="unit_test_lead",
        responsibilities=["Validate test coverage meets 80% threshold"]),
    "integration_test_lead": AgentSpec(agent_id="integration_test_lead",
        name="Integration Test Lead", department="qa", layer=4, role="lead",
        parent_agent_id="qa_head",
        responsibilities=["Coordinate integration test writer"]),
    "integration_test_writer_worker": AgentSpec(agent_id="integration_test_writer_worker",
        name="Integration Test Writer", department="qa", layer=5, role="worker",
        parent_agent_id="integration_test_lead",
        responsibilities=["Generate API integration tests for all OpenAPI endpoints"]),
    "regression_test_lead": AgentSpec(agent_id="regression_test_lead",
        name="Regression Test Lead", department="qa", layer=4, role="lead",
        parent_agent_id="qa_head", responsibilities=["Coordinate regression suite worker"]),
    "regression_suite_worker": AgentSpec(agent_id="regression_suite_worker",
        name="Regression Suite Worker", department="qa", layer=5, role="worker",
        parent_agent_id="regression_test_lead",
        responsibilities=["Run full existing test suite to detect regressions"]),
    "performance_test_lead": AgentSpec(agent_id="performance_test_lead",
        name="Performance Test Lead", department="qa", layer=4, role="lead",
        parent_agent_id="qa_head", responsibilities=["Coordinate performance test worker"]),
    "performance_test_worker": AgentSpec(agent_id="performance_test_worker",
        name="Performance Test Worker", department="qa", layer=5, role="worker",
        parent_agent_id="performance_test_lead",
        responsibilities=["Run load tests on critical paths (p95 < 500ms)"]),

    # ── security-service (9 agents) ───────────────────────────
    "security_head": AgentSpec(agent_id="security_head", name="Security Head",
        department="security", layer=3, role="head", parent_agent_id="manager_agent",
        responsibilities=["CVE scanning, OWASP, secret detection — hard block on Critical/High"],
        default_model="claude-opus-4-6"),
    "dependency_scan_lead": AgentSpec(agent_id="dependency_scan_lead",
        name="Dependency Scan Lead", department="security", layer=4, role="lead",
        parent_agent_id="security_head", responsibilities=["Coordinate CVE scanner"]),
    "cve_scanner_worker": AgentSpec(agent_id="cve_scanner_worker", name="CVE Scanner",
        department="security", layer=5, role="worker",
        parent_agent_id="dependency_scan_lead",
        responsibilities=["Scan all dependencies against CVE databases"]),
    "code_security_lead": AgentSpec(agent_id="code_security_lead",
        name="Code Security Lead", department="security", layer=4, role="lead",
        parent_agent_id="security_head",
        responsibilities=["Coordinate OWASP, Secret Scanner, Injection Check workers"]),
    "owasp_checker_worker": AgentSpec(agent_id="owasp_checker_worker",
        name="OWASP Checker", department="security", layer=5, role="worker",
        parent_agent_id="code_security_lead",
        responsibilities=["Check code against OWASP Top 10 vulnerability patterns"]),
    "secret_scanner_worker": AgentSpec(agent_id="secret_scanner_worker",
        name="Secret Scanner", department="security", layer=5, role="worker",
        parent_agent_id="code_security_lead",
        responsibilities=["Scan for accidentally committed secrets"]),
    "injection_check_worker": AgentSpec(agent_id="injection_check_worker",
        name="Injection Check Worker", department="security", layer=5, role="worker",
        parent_agent_id="code_security_lead",
        responsibilities=["Check for SQL/command/XSS injection vulnerabilities"]),
    "compliance_lead": AgentSpec(agent_id="compliance_lead", name="Compliance Lead",
        department="security", layer=4, role="lead", parent_agent_id="security_head",
        responsibilities=["Coordinate compliance validation worker"]),
    "compliance_validator_worker": AgentSpec(agent_id="compliance_validator_worker",
        name="Compliance Validator", department="security", layer=5, role="worker",
        parent_agent_id="compliance_lead",
        responsibilities=["Validate compliance checklist against project requirements"]),

    # ── devops-service (10 agents) ────────────────────────────
    "devops_head": AgentSpec(agent_id="devops_head", name="DevOps Head",
        department="devops", layer=3, role="head", parent_agent_id="manager_agent",
        responsibilities=["Dockerfiles, CI/CD, deployment, health checks, auto-rollback"]),
    "container_lead": AgentSpec(agent_id="container_lead", name="Container Lead",
        department="devops", layer=4, role="lead", parent_agent_id="devops_head",
        responsibilities=["Coordinate Dockerfile and Docker Compose workers"]),
    "dockerfile_writer_worker": AgentSpec(agent_id="dockerfile_writer_worker",
        name="Dockerfile Writer", department="devops", layer=5, role="worker",
        parent_agent_id="container_lead",
        responsibilities=["Generate optimized minimal Dockerfiles per service"]),
    "docker_compose_worker": AgentSpec(agent_id="docker_compose_worker",
        name="Docker Compose Worker", department="devops", layer=5, role="worker",
        parent_agent_id="container_lead",
        responsibilities=["Generate docker-compose.yml with all services and volumes"]),
    "cicd_lead": AgentSpec(agent_id="cicd_lead", name="CI/CD Lead",
        department="devops", layer=4, role="lead", parent_agent_id="devops_head",
        responsibilities=["Coordinate pipeline config and environment config workers"]),
    "pipeline_config_worker": AgentSpec(agent_id="pipeline_config_worker",
        name="Pipeline Config Worker", department="devops", layer=5, role="worker",
        parent_agent_id="cicd_lead",
        responsibilities=["Generate GitHub Actions CI/CD pipeline configuration"]),
    "environment_config_worker": AgentSpec(agent_id="environment_config_worker",
        name="Environment Config Worker", department="devops", layer=5, role="worker",
        parent_agent_id="cicd_lead",
        responsibilities=["Generate .env.example templates with descriptions"]),
    "infrastructure_ops_lead": AgentSpec(agent_id="infrastructure_ops_lead",
        name="Infrastructure Ops Lead", department="devops", layer=4, role="lead",
        parent_agent_id="devops_head",
        responsibilities=["Coordinate provisioner and health check workers"]),
    "provisioner_worker": AgentSpec(agent_id="provisioner_worker",
        name="Provisioner Worker", department="devops", layer=5, role="worker",
        parent_agent_id="infrastructure_ops_lead",
        responsibilities=["Execute cloud/VPS resource provisioning"]),
    "health_check_worker": AgentSpec(agent_id="health_check_worker",
        name="Health Check Worker", department="devops", layer=5, role="worker",
        parent_agent_id="infrastructure_ops_lead",
        responsibilities=["Validate all services respond post-deployment"]),

    # ── docs-service (7 agents) ───────────────────────────────
    "docs_head": AgentSpec(agent_id="docs_head", name="Docs Head",
        department="docs", layer=3, role="head", parent_agent_id="manager_agent",
        responsibilities=["Run documentation — non-blocking, concurrent with all phases"]),
    "api_docs_lead": AgentSpec(agent_id="api_docs_lead", name="API Docs Lead",
        department="docs", layer=4, role="lead", parent_agent_id="docs_head",
        responsibilities=["Coordinate API docs and SDK docs workers"]),
    "openapi_docs_worker": AgentSpec(agent_id="openapi_docs_worker",
        name="OpenAPI Docs Worker", department="docs", layer=5, role="worker",
        parent_agent_id="api_docs_lead",
        responsibilities=["Generate full API documentation from OpenAPI spec"]),
    "code_docs_lead": AgentSpec(agent_id="code_docs_lead", name="Code Docs Lead",
        department="docs", layer=4, role="lead", parent_agent_id="docs_head",
        responsibilities=["Coordinate code comment and README workers"]),
    "readme_writer_worker": AgentSpec(agent_id="readme_writer_worker",
        name="README Writer", department="docs", layer=5, role="worker",
        parent_agent_id="code_docs_lead",
        responsibilities=["Generate project README"]),
    "user_guide_lead": AgentSpec(agent_id="user_guide_lead", name="User Guide Lead",
        department="docs", layer=4, role="lead", parent_agent_id="docs_head",
        responsibilities=["Coordinate user guide and changelog writers"]),
    "changelog_writer_worker": AgentSpec(agent_id="changelog_writer_worker",
        name="Changelog Writer", department="docs", layer=5, role="worker",
        parent_agent_id="user_guide_lead",
        responsibilities=["Maintain changelog — updated on every version increment"]),

    # ── monitoring-service (10 agents) — M3.7 ─────────────────
    "monitoring_head": AgentSpec(agent_id="monitoring_head", name="Monitoring Head",
        department="monitoring", layer=3, role="head", parent_agent_id="manager_agent",
        responsibilities=["Own the continuous monitoring cycle end-to-end; publish health score"]),
    "metrics_lead": AgentSpec(agent_id="metrics_lead", name="Metrics Lead",
        department="monitoring", layer=4, role="lead", parent_agent_id="monitoring_head",
        responsibilities=["Coordinate infrastructure and application metrics workers"]),
    "infrastructure_metrics_worker": AgentSpec(agent_id="infrastructure_metrics_worker",
        name="Infrastructure Metrics Worker", department="monitoring", layer=5, role="worker",
        parent_agent_id="metrics_lead",
        responsibilities=["Collect Postgres/Qdrant/NATS/Docker samples via providers"]),
    "application_metrics_worker": AgentSpec(agent_id="application_metrics_worker",
        name="Application Metrics Worker", department="monitoring", layer=5, role="worker",
        parent_agent_id="metrics_lead",
        responsibilities=["Collect agent runtime/LLM/WebSocket/repository/deployment samples"]),
    "observability_lead": AgentSpec(agent_id="observability_lead", name="Observability Lead",
        department="monitoring", layer=4, role="lead", parent_agent_id="monitoring_head",
        responsibilities=["Coordinate log and trace analysis workers"]),
    "log_analysis_worker": AgentSpec(agent_id="log_analysis_worker", name="Log Analysis Worker",
        department="monitoring", layer=5, role="worker", parent_agent_id="observability_lead",
        responsibilities=["Aggregate structlog output; surface anomaly/error-rate signals"]),
    "trace_analysis_worker": AgentSpec(agent_id="trace_analysis_worker", name="Trace Analysis Worker",
        department="monitoring", layer=5, role="worker", parent_agent_id="observability_lead",
        responsibilities=["Read OTel spans for latency/error hotspots across services"]),
    "alerting_lead": AgentSpec(agent_id="alerting_lead", name="Alerting Lead",
        department="monitoring", layer=4, role="lead", parent_agent_id="monitoring_head",
        responsibilities=["Coordinate alert and dashboard workers; own severity classification"]),
    "alert_worker": AgentSpec(agent_id="alert_worker", name="Alert Worker",
        department="monitoring", layer=5, role="worker", parent_agent_id="alerting_lead",
        responsibilities=["Evaluate thresholds, dedupe, emit monitoring.alert/.warning"]),
    "dashboard_worker": AgentSpec(agent_id="dashboard_worker", name="Dashboard Worker",
        department="monitoring", layer=5, role="worker", parent_agent_id="alerting_lead",
        responsibilities=["Render dashboard_configuration, export Grafana JSON"]),

    # ── incident-response-service (10 agents) — M3.8 ─────────
    "incident_response_head": AgentSpec(agent_id="incident_response_head", name="Incident Response Head",
        department="incident_response", layer=3, role="head", parent_agent_id="manager_agent",
        responsibilities=[
            "Own one incident's lifecycle end-to-end, from intake to closure",
            "Coordinate Incident Analysis, Recovery, and Communication leads",
            "Decide final incident status and publish incident.resolved/incident.phase.completed",
        ]),
    "incident_analysis_lead": AgentSpec(agent_id="incident_analysis_lead", name="Incident Analysis Lead",
        department="incident_response", layer=4, role="lead", parent_agent_id="incident_response_head",
        responsibilities=["Coordinate Incident Classifier and Evidence Collection workers"]),
    "incident_classifier_worker": AgentSpec(agent_id="incident_classifier_worker", name="Incident Classifier",
        department="incident_response", layer=5, role="worker", parent_agent_id="incident_analysis_lead",
        responsibilities=["Classify incident severity and determine the required recovery action"]),
    "evidence_collection_worker": AgentSpec(agent_id="evidence_collection_worker", name="Evidence Collection Worker",
        department="incident_response", layer=5, role="worker", parent_agent_id="incident_analysis_lead",
        responsibilities=["Gather correlated evidence from Monitoring/DevOps/Repository read-only providers"]),
    "recovery_lead": AgentSpec(agent_id="recovery_lead", name="Recovery Lead",
        department="incident_response", layer=4, role="lead", parent_agent_id="incident_response_head",
        responsibilities=["Coordinate Rollback and Recovery workers; own the recovery_plan artifact"]),
    "rollback_worker": AgentSpec(agent_id="rollback_worker", name="Rollback Worker",
        department="incident_response", layer=5, role="worker", parent_agent_id="recovery_lead",
        responsibilities=["Trigger DevOps rollback via the DevOps Service's own rollback endpoint"]),
    "recovery_worker": AgentSpec(agent_id="recovery_worker", name="Recovery Worker",
        department="incident_response", layer=5, role="worker", parent_agent_id="recovery_lead",
        responsibilities=["Execute non-rollback recovery steps and verify recovery outcome"]),
    "communication_lead": AgentSpec(agent_id="communication_lead", name="Communication Lead",
        department="incident_response", layer=4, role="lead", parent_agent_id="incident_response_head",
        responsibilities=["Coordinate Notification and Reporting workers"]),
    "notification_worker": AgentSpec(agent_id="notification_worker", name="Notification Worker",
        department="incident_response", layer=5, role="worker", parent_agent_id="communication_lead",
        responsibilities=["Notify stakeholders of incident status changes"]),
    "reporting_worker": AgentSpec(agent_id="reporting_worker", name="Reporting Worker",
        department="incident_response", layer=5, role="worker", parent_agent_id="communication_lead",
        responsibilities=["Generate root_cause_analysis, remediation_plan, and incident_report artifacts"]),
}


# ═══════════════════════════════════════════════════════════════
# AGENT FACTORY
# ═══════════════════════════════════════════════════════════════

class AgentFactory:
    """
    Creates agent instances with all dependencies injected.
    The only way to instantiate agents in AASC.

    Usage:
        factory = AgentFactory(db_factory, nats, storage, audit_repo,
                               artifact_repo, token_repo)
        agent   = factory.create("requirements_writer_worker")
        result  = await agent.run(task)
    """

    # Class-level registry maps agent_id → concrete class
    _class_registry: Dict[str, Type] = {}

    def __init__(
        self,
        db_factory:    Callable,
        nats:          Any,
        storage:       Any,
        audit_repo:    Any,
        artifact_repo: Any,
        token_repo:    Any,
        qdrant:        Any = None,
    ):
        self._db_factory    = db_factory
        self._nats          = nats
        self._storage       = storage
        self._audit_repo    = audit_repo
        self._artifact_repo = artifact_repo
        self._token_repo    = token_repo
        self._qdrant        = qdrant

    def create(self, agent_id: str) -> Any:
        """
        Creates an agent instance by agent_id.
        Raises ValueError if agent_id is unknown.
        """
        spec = AGENT_REGISTRY.get(agent_id)
        if not spec:
            raise ValueError(
                f"AgentFactory: unknown agent_id '{agent_id}'. "
                f"Known agents: {sorted(AGENT_REGISTRY.keys())}"
            )

        cls = self._class_registry.get(agent_id)
        if not cls:
            # Lazy load the concrete implementation
            cls = self._load_class(agent_id, spec)

        agent = cls.__new__(cls)
        # Inject spec attributes
        agent.agent_id         = spec.agent_id
        agent.name             = spec.name
        agent.department       = spec.department
        agent.layer            = spec.layer
        agent.role             = spec.role
        agent.responsibilities = spec.responsibilities
        # Inject infrastructure
        agent._db_factory      = self._db_factory
        agent._nats            = self._nats
        agent._storage         = self._storage
        agent._audit_repo      = self._audit_repo
        agent._artifact_repo   = self._artifact_repo
        agent._token_repo      = self._token_repo
        agent._qdrant          = self._qdrant

        log.debug("agent_created", agent_id=agent_id, layer=spec.layer)
        return agent

    @classmethod
    def register(cls, agent_id: str):
        """
        Decorator. Registers a concrete class with the factory.
        Usage: @AgentFactory.register("requirements_writer_worker")
        """
        def decorator(agent_class: Type):
            cls._class_registry[agent_id] = agent_class
            return agent_class
        return decorator

    def _load_class(self, agent_id: str, spec: AgentSpec) -> Type:
        """
        Lazily imports the concrete class from the department module.
        Falls back to GenericAgent if no concrete class is registered.
        """
        from core.runtime.base_agent import BaseAgent

        # Try to import from the department's agents module
        dept_map = {
            "product":      "services.product.agents",
            "architecture": "services.architecture.agents",
            "engineering":  "services.engineering.agents",
            "qa":           "services.qa.agents",
            "security":     "services.security.agents",
            "devops":       "services.devops.agents",
            "docs":         "services.docs.agents",
            "manager":      "services.manager.agents",
            "monitoring":   "services.monitoring.agents",
            "incident_response": "services.incident_response.agents",
        }
        module_path = dept_map.get(spec.department)
        if module_path:
            try:
                import importlib
                module = importlib.import_module(module_path)
                cls_name = _agent_id_to_class_name(agent_id)
                if hasattr(module, cls_name):
                    concrete = getattr(module, cls_name)
                    self._class_registry[agent_id] = concrete
                    return concrete
            except (ImportError, AttributeError):
                pass

        # Fall back to GenericAgent for unimplemented agents
        generic = _make_generic_agent(spec)
        self._class_registry[agent_id] = generic
        return generic

    @staticmethod
    def list_agents() -> List[str]:
        return sorted(AGENT_REGISTRY.keys())

    @staticmethod
    def agents_by_department(department: str) -> List[AgentSpec]:
        return [s for s in AGENT_REGISTRY.values() if s.department == department]


def _agent_id_to_class_name(agent_id: str) -> str:
    """Converts 'requirements_writer_worker' → 'RequirementsWriterWorker'"""
    return "".join(part.capitalize() for part in agent_id.split("_"))


def _make_generic_agent(spec: AgentSpec) -> Type:
    """
    Creates a minimal runnable agent for any unimplemented spec.
    Produces a placeholder output and logs a warning.
    Used during development to keep the system runnable
    while concrete agents are implemented one by one.
    """
    from core.runtime.base_agent import BaseAgent
    from core.contracts import AgentResult, TaskStatus

    class GenericAgent(BaseAgent):
        async def execute(self, task):
            log.warning("generic_agent_executing",
                        agent_id=spec.agent_id,
                        message="No concrete implementation — returning placeholder")
            return AgentResult(
                task_id=task.task_id,
                agent_id=spec.agent_id,
                status=TaskStatus.COMPLETED,
                content={"placeholder": True, "agent": spec.agent_id,
                         "message": f"{spec.name} has no concrete implementation yet."},
                summary=f"[placeholder] {spec.name} ran without implementation",
                quality_score=0.0,
            )

    GenericAgent.__name__ = _agent_id_to_class_name(spec.agent_id)
    return GenericAgent

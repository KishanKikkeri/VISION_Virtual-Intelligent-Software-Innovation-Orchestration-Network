-- ============================================================
-- AASC — PostgreSQL Schema v1
-- Project  : Autonomous AI Software Company
-- Status   : Locked
-- Date     : 2026-06-08
-- Tables   : 38
-- Next doc : LangGraph Workflow Definitions v1
-- ============================================================
--
-- DESIGN RULES
--   1. All primary keys are UUID (gen_random_uuid()).
--   2. All timestamps are TIMESTAMPTZ (timezone-aware).
--   3. JSONB is used for structured variable-length data.
--   4. Append-only tables are marked — never UPDATE or DELETE.
--   5. Status fields use VARCHAR + CHECK constraints (not enums)
--      so new values can be added without ALTER TYPE.
--   6. Soft deletes via is_active or status — no hard deletes
--      on business entities.
--   7. Every table that references projects(id) cascades on delete
--      only where the child data is meaningless without the project.
--   8. The requirement_dependencies table replaces Neo4j for V1.
--      Use recursive CTEs for graph traversal.
-- ============================================================


-- ============================================================
-- SECTION 0 — EXTENSIONS
-- PostgreSQL 13+ has gen_random_uuid() built-in (no extension).
-- Enable pg_trgm for future full-text search on descriptions.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- ============================================================
-- SECTION 1 — CROSS-CUTTING TABLES
-- Shared across all services. No single service "owns" these;
-- they are managed by the platform layer.
-- ============================================================


-- ─── users ───────────────────────────────────────────────────
-- Platform users. JWT auth — no Keycloak in V1.
-- Roles follow the charter: owner > admin > developer > reviewer > observer.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE users (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    email         VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name     VARCHAR(255),
    role          VARCHAR(50)  NOT NULL DEFAULT 'developer'
                  CHECK (role IN ('owner', 'admin', 'developer', 'reviewer', 'observer')),
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    last_login_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_users_email UNIQUE (email)
);


-- ─── agents ──────────────────────────────────────────────────
-- Registry of every agent definition in the system.
-- Populated at startup from the agent configuration.
-- agent_id is the stable string identifier used in all messages
-- and foreign keys (e.g. "backend_api_worker").
-- ─────────────────────────────────────────────────────────────
CREATE TABLE agents (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        VARCHAR(100) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    department      VARCHAR(100) NOT NULL
                    CHECK (department IN (
                        'manager', 'product', 'architecture',
                        'engineering', 'qa', 'security', 'devops', 'docs'
                    )),
    layer           INTEGER      NOT NULL CHECK (layer IN (2, 3, 4, 5)),
    role            VARCHAR(50)  NOT NULL CHECK (role IN ('manager', 'head', 'lead', 'worker')),
    parent_agent_id VARCHAR(100) REFERENCES agents(agent_id) ON DELETE SET NULL,
    default_provider VARCHAR(50),          -- openai | anthropic | gemini | ollama | openrouter
    default_model    VARCHAR(100),
    is_active        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_agents_agent_id UNIQUE (agent_id)
);


-- ─── agent_prompts ────────────────────────────────────────────
-- Versioned system prompts per agent.
-- Only one version per agent can be active at a time.
-- When a prompt is updated: set old is_active = FALSE, insert new row.
-- Never update prompt text — always insert a new version.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE agent_prompts (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id             VARCHAR(100) NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    version              INTEGER      NOT NULL DEFAULT 1,
    system_prompt        TEXT         NOT NULL,
    user_prompt_template TEXT,
    is_active            BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_agent_prompts_agent_version UNIQUE (agent_id, version)
);


-- ─── artifacts ────────────────────────────────────────────────
-- Central registry for every generated artifact across all services.
-- Stores metadata; actual content is in storage_ref (path/URL)
-- or inline in content (JSONB) for small structured artifacts.
-- Lifecycle: draft → under_review → approved / rejected → superseded
-- project_id FK is added after projects table is created (below).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE artifacts (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID         NOT NULL,   -- FK: REFERENCES projects(id) — added below
    artifact_type VARCHAR(100) NOT NULL,
    version       INTEGER      NOT NULL DEFAULT 1,
    created_by    VARCHAR(100) NOT NULL,   -- agent_id string
    approved_by   UUID         REFERENCES users(id) ON DELETE SET NULL,
    status        VARCHAR(50)  NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'under_review', 'approved', 'rejected', 'superseded')),
    storage_ref   TEXT,                    -- S3 key, GitHub path, or local path
    content       JSONB,                   -- inline content for small structured artifacts
    metadata      JSONB        NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_artifacts_project_type_version UNIQUE (project_id, artifact_type, version)
);


-- ─── token_ledger ─────────────────────────────────────────────
-- APPEND-ONLY. Never UPDATE or DELETE rows.
-- Records every LLM API call with tokens and cost.
-- Used by manager-service to enforce budget_limits.
-- total_tokens is a generated (computed) column.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE token_ledger (
    id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id   UUID          NOT NULL,   -- FK added below
    agent_run_id UUID,                     -- FK added below
    agent_id     VARCHAR(100)  NOT NULL,
    department   VARCHAR(100)  NOT NULL,
    provider     VARCHAR(50)   NOT NULL,
    model        VARCHAR(100)  NOT NULL,
    input_tokens  INTEGER      NOT NULL CHECK (input_tokens  >= 0),
    output_tokens INTEGER      NOT NULL CHECK (output_tokens >= 0),
    total_tokens  INTEGER      GENERATED ALWAYS AS (input_tokens + output_tokens) STORED,
    cost_usd      DECIMAL(12,6) NOT NULL CHECK (cost_usd >= 0),
    recorded_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()

    -- APPEND-ONLY: No UPDATE or DELETE permitted on this table.
);


-- ─── audit_events ─────────────────────────────────────────────
-- APPEND-ONLY. Never UPDATE or DELETE rows.
-- The black box recorder. Every meaningful system event goes here.
-- Even successful clean operations are logged (not just errors).
-- project_id is nullable to allow system-level events.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE audit_events (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID,                     -- FK added below; nullable for system events
    event_type  VARCHAR(100) NOT NULL,    -- e.g. "task.created", "approval.granted"
    actor_type  VARCHAR(50)  NOT NULL CHECK (actor_type IN ('user', 'agent', 'system')),
    actor_id    VARCHAR(255) NOT NULL,    -- user UUID (as string) or agent_id string
    entity_type VARCHAR(100),            -- "project" | "task" | "artifact" | ...
    entity_id   UUID,
    payload     JSONB        NOT NULL DEFAULT '{}',
    recorded_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()

    -- APPEND-ONLY: No UPDATE or DELETE permitted on this table.
);


-- ============================================================
-- SECTION 2 — MANAGER-SERVICE TABLES
-- ============================================================


-- ─── projects ─────────────────────────────────────────────────
CREATE TABLE projects (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name           VARCHAR(255) NOT NULL,
    description    TEXT         NOT NULL,
    status         VARCHAR(50)  NOT NULL DEFAULT 'initializing'
                   CHECK (status IN (
                       'initializing', 'requirements', 'architecture',
                       'structure', 'implementation', 'testing',
                       'security', 'deployment', 'monitoring',
                       'improvement', 'completed', 'failed', 'paused'
                   )),
    current_phase  INTEGER      NOT NULL DEFAULT 1 CHECK (current_phase BETWEEN 1 AND 10),
    owner_id       UUID         NOT NULL REFERENCES users(id),
    repository_url TEXT,
    llm_provider   VARCHAR(50)  NOT NULL DEFAULT 'openai'
                   CHECK (llm_provider IN ('openai', 'anthropic', 'gemini', 'ollama', 'openrouter', 'azure_openai')),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Deferred FKs for cross-cutting tables that reference projects --
ALTER TABLE artifacts    ADD CONSTRAINT fk_artifacts_project
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE;

ALTER TABLE token_ledger ADD CONSTRAINT fk_token_ledger_project
    FOREIGN KEY (project_id) REFERENCES projects(id);

ALTER TABLE audit_events ADD CONSTRAINT fk_audit_events_project
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;


-- ─── budget_limits ────────────────────────────────────────────
-- One row per project. limit_usd = NULL means unlimited.
-- Status is updated by manager-service as spend accumulates.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE budget_limits (
    id                    UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id            UUID          NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    limit_usd             DECIMAL(10,2),             -- NULL = unlimited
    warning_threshold_pct INTEGER       NOT NULL DEFAULT 80
                          CHECK (warning_threshold_pct BETWEEN 1 AND 100),
    status                VARCHAR(50)   NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'warning', 'exceeded', 'suspended', 'unlimited')),
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_budget_limits_project UNIQUE (project_id)
);


-- ─── workflows ────────────────────────────────────────────────
-- One workflow per project (may extend to multiple in V2).
-- current_phase mirrors projects.current_phase for quick reads.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE workflows (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    current_phase INTEGER     NOT NULL DEFAULT 1 CHECK (current_phase BETWEEN 1 AND 10),
    status        VARCHAR(50) NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'paused', 'awaiting_approval', 'completed', 'failed')),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_workflows_project UNIQUE (project_id)
);


-- ─── workflow_phases ──────────────────────────────────────────
-- One row per phase per project. Pre-populated at project creation
-- (10 rows per project, all status = 'pending').
-- revision_round tracks how many times a phase was rejected and redone.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE workflow_phases (
    id                 UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id        UUID         NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    project_id         UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    phase_number       INTEGER      NOT NULL CHECK (phase_number BETWEEN 1 AND 10),
    phase_name         VARCHAR(100) NOT NULL,
    status             VARCHAR(50)  NOT NULL DEFAULT 'pending'
                       CHECK (status IN (
                           'pending', 'running', 'awaiting_approval',
                           'approved', 'rejected', 'completed', 'failed'
                       )),
    requires_approval  BOOLEAN      NOT NULL DEFAULT FALSE,
    started_at         TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    approved_by        UUID         REFERENCES users(id) ON DELETE SET NULL,
    approved_at        TIMESTAMPTZ,
    rejection_feedback TEXT,
    revision_round     INTEGER      NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_workflow_phases_workflow_phase UNIQUE (workflow_id, phase_number)
);


-- ─── approvals ────────────────────────────────────────────────
-- Each approval request is a new row. Multiple rounds per phase
-- are tracked via revision_round.
-- expires_at: configurable timeout — NULL means no timeout.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE approvals (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workflow_phase_id UUID         NOT NULL REFERENCES workflow_phases(id),
    artifact_type     VARCHAR(100) NOT NULL,
    artifact_ids      UUID[]       NOT NULL DEFAULT '{}',
    status            VARCHAR(50)  NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    requested_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    responded_at      TIMESTAMPTZ,
    responded_by      UUID         REFERENCES users(id) ON DELETE SET NULL,
    feedback          TEXT,
    revision_round    INTEGER      NOT NULL DEFAULT 1,
    expires_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ─── project_versions ─────────────────────────────────────────
-- Snapshot of project state at a given version number.
-- Used for rollback. Created automatically at every approved phase
-- and on major milestone events.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE project_versions (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_number    VARCHAR(20)  NOT NULL,              -- semver: "1.0.0", "1.1.0"
    description       TEXT,
    phase_snapshot    JSONB        NOT NULL DEFAULT '{}', -- full workflow_phases state
    artifact_snapshot JSONB        NOT NULL DEFAULT '{}', -- { artifact_type: artifact_id }
    created_by        VARCHAR(255) NOT NULL,              -- agent_id or user UUID string
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_project_versions_project_version UNIQUE (project_id, version_number)
);


-- ─── agent_runs ───────────────────────────────────────────────
-- Tracks every individual agent execution for a project task.
-- One row per attempt — retries create new rows (not updates).
-- This is the execution history used for debugging and billing.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE agent_runs (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id         UUID         NOT NULL,
    agent_id        VARCHAR(100) NOT NULL REFERENCES agents(agent_id),
    parent_agent_id VARCHAR(100) REFERENCES agents(agent_id),
    department      VARCHAR(100) NOT NULL,
    status          VARCHAR(50)  NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'running', 'completed',
                        'failed', 'escalated', 'dead_lettered'
                    )),
    input_context   JSONB        NOT NULL DEFAULT '{}',
    output_data     JSONB        NOT NULL DEFAULT '{}',
    retry_count     INTEGER      NOT NULL DEFAULT 0,
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    duration_ms     INTEGER,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Deferred FK from token_ledger to agent_runs --
ALTER TABLE token_ledger ADD CONSTRAINT fk_token_ledger_agent_run
    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id) ON DELETE SET NULL;


-- ============================================================
-- SECTION 3 — PRODUCT-SERVICE TABLES
-- ============================================================


-- ─── requirements ─────────────────────────────────────────────
CREATE TABLE requirements (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id UUID         REFERENCES artifacts(id) ON DELETE SET NULL,
    title       VARCHAR(255) NOT NULL,
    description TEXT         NOT NULL,
    priority    VARCHAR(20)  NOT NULL DEFAULT 'should'
                CHECK (priority IN ('must', 'should', 'could', 'wont')),
    category    VARCHAR(100) NOT NULL DEFAULT 'functional'
                CHECK (category IN ('functional', 'non_functional', 'constraint', 'assumption')),
    status      VARCHAR(50)  NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'validated', 'approved', 'rejected')),
    version     INTEGER      NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ─── features ─────────────────────────────────────────────────
CREATE TABLE features (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id UUID         REFERENCES artifacts(id) ON DELETE SET NULL,
    name        VARCHAR(255) NOT NULL,
    description TEXT         NOT NULL,
    priority    VARCHAR(20)  NOT NULL DEFAULT 'should'
                CHECK (priority IN ('must', 'should', 'could', 'wont')),
    version     INTEGER      NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ─── user_stories ─────────────────────────────────────────────
-- Format: "As a [role], I want [action], so that [benefit]."
-- feature_id is nullable — orphan stories are allowed.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE user_stories (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    feature_id  UUID         REFERENCES features(id) ON DELETE SET NULL,
    artifact_id UUID         REFERENCES artifacts(id) ON DELETE SET NULL,
    role        VARCHAR(255) NOT NULL,    -- "As a..."
    action      TEXT         NOT NULL,    -- "I want to..."
    benefit     TEXT         NOT NULL,    -- "So that..."
    priority    VARCHAR(20)  NOT NULL DEFAULT 'should'
                CHECK (priority IN ('must', 'should', 'could', 'wont')),
    status      VARCHAR(50)  NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'validated', 'approved', 'rejected')),
    version     INTEGER      NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ─── acceptance_criteria ──────────────────────────────────────
-- Format: Given [context], When [action], Then [outcome].
-- Every user story should have at least one criterion.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE acceptance_criteria (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_story_id  UUID        NOT NULL REFERENCES user_stories(id) ON DELETE CASCADE,
    artifact_id    UUID        REFERENCES artifacts(id) ON DELETE SET NULL,
    given_context  TEXT        NOT NULL,
    when_action    TEXT        NOT NULL,
    then_outcome   TEXT        NOT NULL,
    version        INTEGER     NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- SECTION 4 — ARCHITECTURE-SERVICE TABLES
-- ============================================================


-- ─── architecture_designs ────────────────────────────────────
-- Stores system architecture as a diagram + structured components.
-- diagram_content is the raw diagram (Mermaid/PlantUML string).
-- components is a JSONB array of service/component definitions.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE architecture_designs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id     UUID        REFERENCES artifacts(id) ON DELETE SET NULL,
    diagram_type    VARCHAR(50) NOT NULL DEFAULT 'mermaid'
                    CHECK (diagram_type IN ('mermaid', 'json', 'plantuml', 'drawio')),
    diagram_content TEXT        NOT NULL,
    components      JSONB       NOT NULL DEFAULT '[]',  -- [{name, type, description, dependencies[]}]
    version         INTEGER     NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ─── api_specifications ───────────────────────────────────────
-- Stores the full API contract (OpenAPI 3.1 by default).
-- spec_content holds the complete parsed spec as JSONB.
-- endpoint_count is a denormalized count for quick display.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE api_specifications (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id    UUID        REFERENCES artifacts(id) ON DELETE SET NULL,
    spec_format    VARCHAR(30) NOT NULL DEFAULT 'openapi_31'
                   CHECK (spec_format IN ('openapi_31', 'openapi_30', 'graphql', 'grpc', 'asyncapi')),
    spec_content   JSONB       NOT NULL,
    endpoint_count INTEGER     NOT NULL DEFAULT 0,
    version        INTEGER     NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ─── db_schemas ───────────────────────────────────────────────
-- The generated DB schema for the user's project (not AASC itself).
-- schema_content: JSONB table definitions.
-- migration_scripts: raw SQL migration file content.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE db_schemas (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id       UUID        REFERENCES artifacts(id) ON DELETE SET NULL,
    schema_content    JSONB       NOT NULL DEFAULT '{}',
    migration_scripts TEXT,
    table_count       INTEGER     NOT NULL DEFAULT 0,
    version           INTEGER     NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ─── infrastructure_plans ────────────────────────────────────
-- Lists all services required by the project: ports, volumes,
-- environment variables, resource estimates.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE infrastructure_plans (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id            UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id           UUID        REFERENCES artifacts(id) ON DELETE SET NULL,
    services              JSONB       NOT NULL DEFAULT '[]',
    deployment_target     VARCHAR(50) NOT NULL DEFAULT 'docker_compose'
                          CHECK (deployment_target IN (
                              'docker_compose', 'kubernetes', 'aws', 'gcp', 'azure', 'vps'
                          )),
    resource_requirements JSONB       NOT NULL DEFAULT '{}',
    version               INTEGER     NOT NULL DEFAULT 1,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- SECTION 5 — ENGINEERING-SERVICE TABLES
-- ============================================================


-- ─── code_modules ─────────────────────────────────────────────
-- Represents a logical unit of generated code (one module = one
-- cohesive set of files). Generated, reviewed, and committed atomically.
-- commit_sha is set once the module is committed to GitHub.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE code_modules (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id   UUID         REFERENCES artifacts(id) ON DELETE SET NULL,
    name          VARCHAR(255) NOT NULL,
    module_type   VARCHAR(50)  NOT NULL
                  CHECK (module_type IN (
                      'api_endpoint', 'model', 'migration', 'component',
                      'page', 'middleware', 'config', 'service', 'utility', 'test'
                  )),
    file_paths    TEXT[]       NOT NULL DEFAULT '{}',
    language      VARCHAR(50)  NOT NULL,
    review_passed BOOLEAN,
    commit_sha    VARCHAR(40),
    status        VARCHAR(50)  NOT NULL DEFAULT 'generating'
                  CHECK (status IN ('generating', 'reviewing', 'approved', 'committed', 'failed')),
    version       INTEGER      NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ─── engineering_tasks ────────────────────────────────────────
-- Atomic unit of work assigned to a worker agent.
-- depends_on: UUID array of task IDs that must complete first.
-- priority: 1 (highest) to 10 (lowest).
-- ─────────────────────────────────────────────────────────────
CREATE TABLE engineering_tasks (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    module_id      UUID         REFERENCES code_modules(id) ON DELETE SET NULL,
    title          VARCHAR(255) NOT NULL,
    description    TEXT         NOT NULL,
    layer          VARCHAR(50)  NOT NULL
                   CHECK (layer IN ('backend', 'frontend', 'integration', 'devops')),
    assigned_agent VARCHAR(100) REFERENCES agents(agent_id) ON DELETE SET NULL,
    priority       INTEGER      NOT NULL DEFAULT 5 CHECK (priority BETWEEN 1 AND 10),
    depends_on     UUID[]       NOT NULL DEFAULT '{}',
    status         VARCHAR(50)  NOT NULL DEFAULT 'pending'
                   CHECK (status IN (
                       'pending', 'in_progress', 'reviewing',
                       'completed', 'failed', 'blocked'
                   )),
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ─── repository_state ────────────────────────────────────────
-- One row per project. Tracks GitHub repository binding.
-- folder_structure: the initialized directory tree as JSONB.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE repository_state (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id       UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    github_repo_url  TEXT,
    github_repo_id   VARCHAR(100),
    default_branch   VARCHAR(100) NOT NULL DEFAULT 'main',
    folder_structure JSONB        NOT NULL DEFAULT '{}',
    is_initialized   BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_repository_state_project UNIQUE (project_id)
);


-- ─── commit_records ───────────────────────────────────────────
-- APPEND-ONLY. One row per git commit.
-- files_changed: list of relative paths modified in this commit.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE commit_records (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID         NOT NULL REFERENCES projects(id),
    module_id      UUID         REFERENCES code_modules(id) ON DELETE SET NULL,
    commit_sha     VARCHAR(40)  NOT NULL,
    commit_message TEXT         NOT NULL,
    committed_by   VARCHAR(100) NOT NULL,   -- agent_id
    branch         VARCHAR(100) NOT NULL DEFAULT 'main',
    files_changed  TEXT[]       NOT NULL DEFAULT '{}',
    committed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()

    -- APPEND-ONLY: No UPDATE or DELETE permitted on this table.
);


-- ============================================================
-- SECTION 6 — QA-SERVICE TABLES
-- ============================================================


-- ─── test_suites ─────────────────────────────────────────────
CREATE TABLE test_suites (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id UUID        REFERENCES artifacts(id) ON DELETE SET NULL,
    module_id   UUID        REFERENCES code_modules(id) ON DELETE SET NULL,
    suite_type  VARCHAR(50) NOT NULL
                CHECK (suite_type IN ('unit', 'integration', 'regression', 'performance', 'e2e')),
    file_paths  TEXT[]      NOT NULL DEFAULT '{}',
    status      VARCHAR(50) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'passed', 'failed')),
    version     INTEGER     NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ─── test_results ─────────────────────────────────────────────
-- APPEND-ONLY. Each test run produces a new row.
-- error_details: [{test_name, error, file, line}]
-- ─────────────────────────────────────────────────────────────
CREATE TABLE test_results (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID        NOT NULL REFERENCES projects(id),
    suite_id      UUID        NOT NULL REFERENCES test_suites(id),
    total_tests   INTEGER     NOT NULL DEFAULT 0,
    passed_tests  INTEGER     NOT NULL DEFAULT 0,
    failed_tests  INTEGER     NOT NULL DEFAULT 0,
    skipped_tests INTEGER     NOT NULL DEFAULT 0,
    error_details JSONB       NOT NULL DEFAULT '[]',
    duration_ms   INTEGER,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()

    -- APPEND-ONLY: No UPDATE or DELETE permitted on this table.
);


-- ─── coverage_records ─────────────────────────────────────────
-- APPEND-ONLY. Each measurement is a new row.
-- meets_threshold is derived from line_coverage >= threshold_pct.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE coverage_records (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id        UUID         NOT NULL REFERENCES projects(id),
    module_id         UUID         REFERENCES code_modules(id) ON DELETE SET NULL,
    suite_id          UUID         REFERENCES test_suites(id) ON DELETE SET NULL,
    line_coverage     DECIMAL(5,2) CHECK (line_coverage     BETWEEN 0 AND 100),
    branch_coverage   DECIMAL(5,2) CHECK (branch_coverage   BETWEEN 0 AND 100),
    function_coverage DECIMAL(5,2) CHECK (function_coverage BETWEEN 0 AND 100),
    meets_threshold   BOOLEAN      NOT NULL DEFAULT FALSE,
    threshold_pct     DECIMAL(5,2) NOT NULL DEFAULT 80.0,
    measured_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()

    -- APPEND-ONLY: No UPDATE or DELETE permitted on this table.
);


-- ============================================================
-- SECTION 7 — SECURITY-SERVICE TABLES
-- ============================================================


-- ─── security_scans ──────────────────────────────────────────
-- One scan per phase completion. finding_counts is denormalized
-- for fast dashboard queries without counting security_findings.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE security_scans (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID        NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id    UUID        REFERENCES artifacts(id) ON DELETE SET NULL,
    scan_type      VARCHAR(50) NOT NULL DEFAULT 'full'
                   CHECK (scan_type IN ('full', 'incremental', 'targeted', 'dependency_only')),
    status         VARCHAR(50) NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'passed', 'failed', 'blocked')),
    finding_counts JSONB       NOT NULL
                   DEFAULT '{"critical":0,"high":0,"medium":0,"low":0,"info":0}',
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ─── security_findings ────────────────────────────────────────
-- Each vulnerability found is one row.
-- status = 'open' with severity in ('critical','high') → blocks deploy.
-- cvss_score: 0.0–10.0 per CVSS v3.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE security_findings (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID         NOT NULL REFERENCES projects(id),
    scan_id         UUID         NOT NULL REFERENCES security_scans(id),
    severity        VARCHAR(20)  NOT NULL
                    CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info')),
    category        VARCHAR(100) NOT NULL,   -- cve | owasp | secret | injection | auth | ...
    title           VARCHAR(255) NOT NULL,
    description     TEXT         NOT NULL,
    file_path       TEXT,
    line_number     INTEGER,
    cve_id          VARCHAR(50),
    cvss_score      DECIMAL(3,1) CHECK (cvss_score BETWEEN 0 AND 10),
    remediation     TEXT,
    status          VARCHAR(50)  NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'acknowledged', 'fixed', 'false_positive', 'wont_fix')),
    acknowledged_by UUID         REFERENCES users(id) ON DELETE SET NULL,
    acknowledged_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ─── compliance_records ───────────────────────────────────────
CREATE TABLE compliance_records (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID         NOT NULL REFERENCES projects(id),
    scan_id     UUID         NOT NULL REFERENCES security_scans(id),
    standard    VARCHAR(100) NOT NULL DEFAULT 'owasp_top10',
    check_name  VARCHAR(255) NOT NULL,
    status      VARCHAR(50)  NOT NULL
                CHECK (status IN ('passed', 'failed', 'skipped', 'not_applicable')),
    details     TEXT,
    checked_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ============================================================
-- SECTION 8 — DEVOPS-SERVICE TABLES
-- ============================================================


-- ─── deployments ──────────────────────────────────────────────
-- One row per deployment attempt. rolled_back_to references
-- the deployment that was restored if this one was rolled back.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE deployments (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id         UUID        NOT NULL REFERENCES projects(id),
    version_id         UUID        REFERENCES project_versions(id) ON DELETE SET NULL,
    deployment_type    VARCHAR(50) NOT NULL DEFAULT 'docker_compose'
                       CHECK (deployment_type IN (
                           'docker_compose', 'kubernetes', 'aws', 'gcp', 'azure', 'vps'
                       )),
    target_environment VARCHAR(50) NOT NULL DEFAULT 'production'
                       CHECK (target_environment IN ('development', 'staging', 'production')),
    status             VARCHAR(50) NOT NULL DEFAULT 'pending'
                       CHECK (status IN (
                           'pending', 'approved', 'running',
                           'completed', 'failed', 'rolled_back'
                       )),
    approved_by        UUID        REFERENCES users(id) ON DELETE SET NULL,
    approved_at        TIMESTAMPTZ,
    started_at         TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    rolled_back_to     UUID        REFERENCES deployments(id) ON DELETE SET NULL,
    error_details      TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ─── deployment_plans ─────────────────────────────────────────
-- Generated by devops-service and submitted for user approval.
-- plan_content: full human-readable deployment plan as JSONB.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE deployment_plans (
    id                         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id                 UUID        NOT NULL REFERENCES projects(id),
    deployment_id              UUID        REFERENCES deployments(id) ON DELETE SET NULL,
    artifact_id                UUID        REFERENCES artifacts(id) ON DELETE SET NULL,
    plan_content               JSONB       NOT NULL DEFAULT '{}',
    services_count             INTEGER     NOT NULL DEFAULT 0,
    estimated_downtime_seconds INTEGER     NOT NULL DEFAULT 0,
    version                    INTEGER     NOT NULL DEFAULT 1,
    status                     VARCHAR(50) NOT NULL DEFAULT 'draft'
                               CHECK (status IN ('draft', 'approved', 'executed', 'superseded')),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ─── infrastructure_configs ───────────────────────────────────
-- Generated configuration files (Dockerfile, docker-compose.yml,
-- GitHub Actions, .env.example). content is the raw file text.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE infrastructure_configs (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID         NOT NULL REFERENCES projects(id),
    deployment_id UUID         REFERENCES deployments(id) ON DELETE SET NULL,
    artifact_id   UUID         REFERENCES artifacts(id) ON DELETE SET NULL,
    config_type   VARCHAR(50)  NOT NULL
                  CHECK (config_type IN (
                      'dockerfile', 'docker_compose', 'github_actions',
                      'env_template', 'kubernetes_manifest', 'terraform'
                  )),
    content       TEXT         NOT NULL,
    file_name     VARCHAR(255) NOT NULL,
    version       INTEGER      NOT NULL DEFAULT 1,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ─── health_checks ────────────────────────────────────────────
-- Post-deployment validation results.
-- services_checked: [{name, url, status, response_ms}]
-- failed_services:  [{name, url, error}]
-- On any failure: deployment is rolled back automatically.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE health_checks (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id       UUID        NOT NULL REFERENCES projects(id),
    deployment_id    UUID        NOT NULL REFERENCES deployments(id),
    status           VARCHAR(50) NOT NULL
                     CHECK (status IN ('passed', 'failed', 'timeout', 'skipped')),
    services_checked JSONB       NOT NULL DEFAULT '[]',
    failed_services  JSONB       NOT NULL DEFAULT '[]',
    checked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- SECTION 9 — DOCS-SERVICE TABLES
-- ============================================================


-- ─── documentation ────────────────────────────────────────────
CREATE TABLE documentation (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id UUID         REFERENCES artifacts(id) ON DELETE SET NULL,
    doc_type    VARCHAR(50)  NOT NULL
                CHECK (doc_type IN (
                    'api_docs', 'readme', 'user_guide',
                    'architecture_doc', 'sdk_docs', 'runbook'
                )),
    title       VARCHAR(255) NOT NULL,
    content     TEXT         NOT NULL,
    format      VARCHAR(20)  NOT NULL DEFAULT 'markdown'
                CHECK (format IN ('markdown', 'html', 'rst')),
    version     INTEGER      NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


-- ─── changelogs ───────────────────────────────────────────────
-- APPEND-ONLY. Each entry is a new row — no updates.
-- entry_type follows Keep a Changelog conventions.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE changelogs (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id     UUID         NOT NULL REFERENCES projects(id),
    version_number VARCHAR(20)  NOT NULL,
    entry_type     VARCHAR(50)  NOT NULL
                   CHECK (entry_type IN (
                       'added', 'changed', 'fixed',
                       'removed', 'security', 'deprecated'
                   )),
    description    TEXT         NOT NULL,
    created_by     VARCHAR(100) NOT NULL,   -- agent_id
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()

    -- APPEND-ONLY: No UPDATE or DELETE permitted on this table.
);


-- ============================================================
-- SECTION 10 — GRAPH / DEPENDENCY TABLE
-- Replaces Neo4j for V1.
-- All relationships between project entities (requirements,
-- features, API specs, modules, test suites) are stored here.
-- Use recursive CTEs for multi-hop traversal.
-- ============================================================

CREATE TABLE requirement_dependencies (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id          UUID         NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_entity_type  VARCHAR(50)  NOT NULL
                        CHECK (source_entity_type IN (
                            'requirement', 'feature', 'user_story', 'acceptance_criteria',
                            'api_spec', 'code_module', 'test_suite', 'deployment'
                        )),
    source_entity_id    UUID         NOT NULL,
    target_entity_type  VARCHAR(50)  NOT NULL
                        CHECK (target_entity_type IN (
                            'requirement', 'feature', 'user_story', 'acceptance_criteria',
                            'api_spec', 'code_module', 'test_suite', 'deployment'
                        )),
    target_entity_id    UUID         NOT NULL,
    relationship_type   VARCHAR(50)  NOT NULL
                        CHECK (relationship_type IN (
                            'requires', 'implements', 'tested_by',
                            'depends_on', 'derived_from', 'satisfies'
                        )),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_req_deps UNIQUE (
        project_id, source_entity_type, source_entity_id,
        target_entity_type, target_entity_id, relationship_type
    )
);

-- ── Recursive CTE example (traceability: requirement → test_suite) ─
--
-- WITH RECURSIVE traceability AS (
--     -- Anchor: start from a requirement
--     SELECT
--         source_entity_id   AS from_id,
--         source_entity_type AS from_type,
--         target_entity_id   AS to_id,
--         target_entity_type AS to_type,
--         relationship_type,
--         1                  AS depth,
--         ARRAY[source_entity_id] AS visited
--     FROM requirement_dependencies
--     WHERE source_entity_id = $requirement_id
--       AND project_id       = $project_id
--
--     UNION ALL
--
--     -- Recursive: follow outbound edges, avoid cycles
--     SELECT
--         rd.source_entity_id,
--         rd.source_entity_type,
--         rd.target_entity_id,
--         rd.target_entity_type,
--         rd.relationship_type,
--         t.depth + 1,
--         t.visited || rd.target_entity_id
--     FROM requirement_dependencies rd
--     JOIN traceability t ON rd.source_entity_id = t.to_id
--     WHERE t.depth      < 10
--       AND rd.project_id = $project_id
--       AND NOT (rd.target_entity_id = ANY(t.visited))   -- cycle guard
-- )
-- SELECT * FROM traceability ORDER BY depth;


-- ============================================================
-- SECTION 11 — INDEXES
-- All FK columns are indexed.
-- Additional indexes target the most common query patterns.
-- ============================================================

-- ── users ─────────────────────────────────────────────────────
CREATE INDEX idx_users_role      ON users(role);
CREATE INDEX idx_users_is_active ON users(is_active) WHERE is_active = TRUE;

-- ── agents ────────────────────────────────────────────────────
CREATE INDEX idx_agents_department ON agents(department);
CREATE INDEX idx_agents_layer      ON agents(layer);
CREATE INDEX idx_agents_parent     ON agents(parent_agent_id);

-- ── agent_prompts ─────────────────────────────────────────────
CREATE INDEX idx_agent_prompts_agent   ON agent_prompts(agent_id);
CREATE INDEX idx_agent_prompts_active  ON agent_prompts(agent_id) WHERE is_active = TRUE;

-- ── artifacts ─────────────────────────────────────────────────
CREATE INDEX idx_artifacts_project      ON artifacts(project_id);
CREATE INDEX idx_artifacts_project_type ON artifacts(project_id, artifact_type);
CREATE INDEX idx_artifacts_status       ON artifacts(status);

-- ── token_ledger ──────────────────────────────────────────────
CREATE INDEX idx_token_ledger_project    ON token_ledger(project_id);
CREATE INDEX idx_token_ledger_time       ON token_ledger(recorded_at DESC);
CREATE INDEX idx_token_ledger_department ON token_ledger(project_id, department);
-- Covering index: fast SUM(cost_usd) per project
CREATE INDEX idx_token_ledger_cost       ON token_ledger(project_id) INCLUDE (cost_usd);

-- ── audit_events ──────────────────────────────────────────────
CREATE INDEX idx_audit_project    ON audit_events(project_id);
CREATE INDEX idx_audit_time       ON audit_events(recorded_at DESC);
CREATE INDEX idx_audit_event_type ON audit_events(event_type);
CREATE INDEX idx_audit_entity     ON audit_events(entity_type, entity_id);
CREATE INDEX idx_audit_actor      ON audit_events(actor_id);

-- ── projects ──────────────────────────────────────────────────
CREATE INDEX idx_projects_owner  ON projects(owner_id);
CREATE INDEX idx_projects_status ON projects(status);

-- ── workflows ─────────────────────────────────────────────────
CREATE INDEX idx_workflows_project ON workflows(project_id);

-- ── workflow_phases ───────────────────────────────────────────
CREATE INDEX idx_wf_phases_workflow ON workflow_phases(workflow_id);
CREATE INDEX idx_wf_phases_status   ON workflow_phases(project_id, status);

-- ── approvals ─────────────────────────────────────────────────
CREATE INDEX idx_approvals_project ON approvals(project_id);
CREATE INDEX idx_approvals_pending  ON approvals(project_id)
    WHERE status = 'pending';

-- ── project_versions ──────────────────────────────────────────
CREATE INDEX idx_project_versions_project ON project_versions(project_id);

-- ── agent_runs ────────────────────────────────────────────────
CREATE INDEX idx_agent_runs_project ON agent_runs(project_id);
CREATE INDEX idx_agent_runs_task    ON agent_runs(task_id);
CREATE INDEX idx_agent_runs_agent   ON agent_runs(agent_id);
CREATE INDEX idx_agent_runs_status  ON agent_runs(project_id, status);

-- ── requirements ──────────────────────────────────────────────
CREATE INDEX idx_requirements_project  ON requirements(project_id);
CREATE INDEX idx_requirements_priority ON requirements(project_id, priority);

-- ── features ──────────────────────────────────────────────────
CREATE INDEX idx_features_project ON features(project_id);

-- ── user_stories ──────────────────────────────────────────────
CREATE INDEX idx_user_stories_project ON user_stories(project_id);
CREATE INDEX idx_user_stories_feature ON user_stories(feature_id);

-- ── acceptance_criteria ───────────────────────────────────────
CREATE INDEX idx_ac_story   ON acceptance_criteria(user_story_id);
CREATE INDEX idx_ac_project ON acceptance_criteria(project_id);

-- ── architecture / api / db / infra ───────────────────────────
CREATE INDEX idx_arch_designs_project ON architecture_designs(project_id);
CREATE INDEX idx_api_specs_project    ON api_specifications(project_id);
CREATE INDEX idx_db_schemas_project   ON db_schemas(project_id);
CREATE INDEX idx_infra_plans_project  ON infrastructure_plans(project_id);

-- ── code_modules ──────────────────────────────────────────────
CREATE INDEX idx_code_modules_project ON code_modules(project_id);
CREATE INDEX idx_code_modules_status  ON code_modules(project_id, status);
CREATE INDEX idx_code_modules_type    ON code_modules(project_id, module_type);

-- ── engineering_tasks ─────────────────────────────────────────
CREATE INDEX idx_eng_tasks_project ON engineering_tasks(project_id);
CREATE INDEX idx_eng_tasks_status  ON engineering_tasks(project_id, status);
CREATE INDEX idx_eng_tasks_module  ON engineering_tasks(module_id);
CREATE INDEX idx_eng_tasks_agent   ON engineering_tasks(assigned_agent);

-- ── commit_records ────────────────────────────────────────────
CREATE INDEX idx_commits_project ON commit_records(project_id);
CREATE INDEX idx_commits_module  ON commit_records(module_id);
CREATE INDEX idx_commits_sha     ON commit_records(commit_sha);

-- ── test_suites ───────────────────────────────────────────────
CREATE INDEX idx_test_suites_project ON test_suites(project_id);
CREATE INDEX idx_test_suites_module  ON test_suites(module_id);
CREATE INDEX idx_test_suites_status  ON test_suites(project_id, status);

-- ── test_results ──────────────────────────────────────────────
CREATE INDEX idx_test_results_suite   ON test_results(suite_id);
CREATE INDEX idx_test_results_time    ON test_results(run_at DESC);
CREATE INDEX idx_test_results_project ON test_results(project_id);

-- ── coverage_records ──────────────────────────────────────────
CREATE INDEX idx_coverage_project ON coverage_records(project_id);
CREATE INDEX idx_coverage_module  ON coverage_records(module_id);

-- ── security_scans ────────────────────────────────────────────
CREATE INDEX idx_sec_scans_project ON security_scans(project_id);
CREATE INDEX idx_sec_scans_status  ON security_scans(project_id, status);

-- ── security_findings ─────────────────────────────────────────
CREATE INDEX idx_sec_findings_scan     ON security_findings(scan_id);
CREATE INDEX idx_sec_findings_severity ON security_findings(project_id, severity);
-- Fast blocking check: open critical/high findings
CREATE INDEX idx_sec_findings_blocking ON security_findings(project_id, severity)
    WHERE severity IN ('critical', 'high') AND status = 'open';

-- ── compliance_records ────────────────────────────────────────
CREATE INDEX idx_compliance_scan ON compliance_records(scan_id);

-- ── deployments ───────────────────────────────────────────────
CREATE INDEX idx_deployments_project ON deployments(project_id);
CREATE INDEX idx_deployments_status  ON deployments(project_id, status);
CREATE INDEX idx_deployments_latest  ON deployments(project_id, created_at DESC);

-- ── deployment_plans ──────────────────────────────────────────
CREATE INDEX idx_deploy_plans_project ON deployment_plans(project_id);

-- ── infrastructure_configs ────────────────────────────────────
CREATE INDEX idx_infra_configs_project    ON infrastructure_configs(project_id);
CREATE INDEX idx_infra_configs_deployment ON infrastructure_configs(deployment_id);

-- ── health_checks ─────────────────────────────────────────────
CREATE INDEX idx_health_checks_deployment ON health_checks(deployment_id);
CREATE INDEX idx_health_checks_project    ON health_checks(project_id);

-- ── documentation ─────────────────────────────────────────────
CREATE INDEX idx_docs_project ON documentation(project_id);
CREATE INDEX idx_docs_type    ON documentation(project_id, doc_type);

-- ── changelogs ────────────────────────────────────────────────
CREATE INDEX idx_changelogs_project ON changelogs(project_id);
CREATE INDEX idx_changelogs_version ON changelogs(project_id, version_number);

-- ── requirement_dependencies ──────────────────────────────────
CREATE INDEX idx_req_deps_project ON requirement_dependencies(project_id);
CREATE INDEX idx_req_deps_source  ON requirement_dependencies(project_id, source_entity_type, source_entity_id);
CREATE INDEX idx_req_deps_target  ON requirement_dependencies(project_id, target_entity_type, target_entity_id);


-- ============================================================
-- SCHEMA SUMMARY
-- ============================================================
--
--  Section 1 — Cross-cutting (6 tables)
--    users, agents, agent_prompts, artifacts,
--    token_ledger, audit_events
--
--  Section 2 — manager-service (7 tables)
--    projects, budget_limits, workflows, workflow_phases,
--    approvals, project_versions, agent_runs
--
--  Section 3 — product-service (4 tables)
--    requirements, features, user_stories, acceptance_criteria
--
--  Section 4 — architecture-service (4 tables)
--    architecture_designs, api_specifications,
--    db_schemas, infrastructure_plans
--
--  Section 5 — engineering-service (4 tables)
--    code_modules, engineering_tasks,
--    repository_state, commit_records
--
--  Section 6 — qa-service (3 tables)
--    test_suites, test_results, coverage_records
--
--  Section 7 — security-service (3 tables)
--    security_scans, security_findings, compliance_records
--
--  Section 8 — devops-service (4 tables)
--    deployments, deployment_plans,
--    infrastructure_configs, health_checks
--
--  Section 9 — docs-service (2 tables)
--    documentation, changelogs
--
--  Section 10 — Graph (1 table)
--    requirement_dependencies
--
--  TOTAL: 38 tables  |  ~95 indexes
--
-- Append-only tables (7):
--    token_ledger, audit_events, commit_records,
--    test_results, coverage_records, changelogs
--    (health_checks are also treated as append-only in practice)
--
-- ============================================================
-- END OF AASC_PostgreSQL_Schema_v1.sql
-- Next: LangGraph Workflow Definitions v1
-- ============================================================

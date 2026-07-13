"""
services/monitoring/integration/platform_anchor.py
=========================================================
Architectural finding discovered during implementation (documented per
the handover's "Only modify Manager if a genuine orchestration issue
is discovered — document every such change" rule, extended here since
this is a genuine cross-cutting discovery, not a Manager change):

Every prior department (M2–M3.6) is invoked *within* a single project's
workflow, so `TaskInput.project_id` / `AgentContext.project_id` and the
`artifacts.project_id` FK (NOT NULL → projects.id, which itself has a
NOT NULL FK `owner_id → users.id`) were never a problem — there was
always a real project already in flight.

Monitoring is the platform's first cross-project, continuously-running
department. It has no single project to anchor to, but the existing
Artifact Registry schema requires one (and the spec's Constraints
section forbids modifying existing schemas "unless absolutely
necessary" — and it is not necessary here, since a sentinel row solves
it without touching a single existing table or constraint).

Resolution (frozen as spec §0 Decision 6, added post-hoc — this file
is the canonical reference): at Monitoring Service startup, idempotently
ensure exactly one system user and one "Platform Monitoring" project
exist, and anchor every Monitoring artifact/task to that project_id.
This is one additive row in two already-existing tables — no schema
change, no modification to AgentFactory/BaseAgent/other departments.
"""
from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select

from infrastructure.database.models import Project, User

PLATFORM_USER_EMAIL   = "monitoring@system.internal"
PLATFORM_PROJECT_NAME = "Platform Monitoring"


async def ensure_platform_anchor(db_factory: Any) -> str:
    """
    Idempotently ensures the sentinel system user + project exist and
    returns the project_id every Monitoring TaskInput/AgentContext and
    artifact should use. Safe to call on every startup — a second call
    finds the existing rows and does nothing.
    """
    async with db_factory() as db:
        user_result = await db.execute(select(User).where(User.email == PLATFORM_USER_EMAIL))
        user = user_result.scalar_one_or_none()
        if user is None:
            # No login is ever performed as this user — password_hash is
            # an unusable placeholder, not a real credential.
            user = User(
                email=PLATFORM_USER_EMAIL,
                password_hash=hashlib.sha256(b"unusable-system-account").hexdigest(),
                full_name="Platform Monitoring (system)",
                role="observer",
                is_active=True,
            )
            db.add(user)
            await db.flush()

        proj_result = await db.execute(select(Project).where(Project.name == PLATFORM_PROJECT_NAME))
        project = proj_result.scalar_one_or_none()
        if project is None:
            project = Project(
                name=PLATFORM_PROJECT_NAME,
                description="Sentinel project anchoring cross-project Monitoring Service "
                            "artifacts and tasks — not a real software project.",
                status="active",
                current_phase=1,
                owner_id=user.id,
            )
            db.add(project)
            await db.flush()

        return project.id

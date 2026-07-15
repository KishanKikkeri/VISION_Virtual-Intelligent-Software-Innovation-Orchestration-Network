"""
tests/foundation/test_m32_repository.py
==========================================
M3.2 Repository Service tests — 4 layers matching the Phase 2/M3.1 pattern.

Layer 1 — Unit:        naming policy, commit formatting, provider status mapping
Layer 2 — Graph:       routing functions (no provider/DB)
Layer 3 — Integration: managers against a fake provider + in-memory fake DB
Layer 4 — Failure:     GitHub unavailable, merge conflict, duplicate tag,
                       invalid branch names, permission failures
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import httpx
import pytest

from services.repository.managers import (
    RepositoryDeps, assert_not_protected, build_branch_name, slugify,
)
from services.repository.managers.audit_manager import AuditManager
from services.repository.managers.branch_manager import BranchManager
from services.repository.managers.commit_manager import CommitManager, format_commit_message
from services.repository.managers.pull_request_manager import PullRequestManager
from services.repository.managers.release_manager import ReleaseManager
from services.repository.managers.repository_manager import RepositoryManager
from services.repository.providers.base_provider import BaseRepositoryProvider
from services.repository.providers.github_provider import GitHubProvider
from services.repository.schemas import (
    ApprovePullRequestRequest,
    BranchType,
    CommitFilesRequest,
    CommitMetadata,
    CreateBranchRequest,
    CreatePullRequestRequest,
    CreateReleaseRequest,
    CreateRepositoryRequest,
    DuplicateTagError,
    FileChange,
    InvalidBranchNameError,
    MergeConflictError,
    MergePullRequestRequest,
    PermissionDeniedError,
    ProtectedBranchViolationError,
    ProviderBranchResult,
    ProviderCommitResult,
    ProviderPullRequestResult,
    ProviderRepoResult,
    ProviderReleaseResult,
    ProviderUnavailableError,
    RepositoryServiceError,
    RepositoryVisibility,
    RollbackReleaseRequest,
)
from services.repository.workflows.repository_graph import (
    route_after_commit,
    route_after_create_branch,
    route_after_merge,
    route_after_open_pr,
    route_after_validate,
    route_approval_gate,
)


# ══════════════════════════════════════════════════════════════
# Shared fakes — in-memory DB tables + configurable fake provider
# ══════════════════════════════════════════════════════════════

def _row(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


class FakeDB:
    """Stand-in AsyncSession — none of the fakes below touch it."""


@asynccontextmanager
async def _fake_db_factory():
    yield FakeDB()


class FakeRepositoryTable:
    def __init__(self):
        self.rows: Dict[str, SimpleNamespace] = {}

    async def create(self, db, project_id, provider, owner, name, full_name,
                     default_branch="main", clone_url=None, html_url=None,
                     visibility="private", provider_repo_id=None, metadata=None):
        row = _row(id=str(uuid.uuid4()), project_id=project_id, provider=provider,
                   owner=owner, name=name, full_name=full_name,
                   default_branch=default_branch, clone_url=clone_url, html_url=html_url,
                   visibility=visibility, status="active",
                   provider_repo_id=provider_repo_id, metadata_=metadata or {},
                   created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        self.rows[row.id] = row
        return row

    async def get_by_id(self, db, repository_id):
        return self.rows.get(repository_id)

    async def get_by_project(self, db, project_id):
        return next((r for r in self.rows.values() if r.project_id == project_id), None)

    async def update_status(self, db, repository_id, status):
        if repository_id in self.rows:
            self.rows[repository_id].status = status


class FakeBranchTable:
    def __init__(self):
        self.rows: Dict[str, SimpleNamespace] = {}

    async def create(self, db, repository_id, name, branch_type="feature", task_id=None,
                     base_branch="develop", head_sha=None, is_protected=False,
                     created_by="VISION Bot"):
        row = _row(id=str(uuid.uuid4()), repository_id=repository_id, name=name,
                   branch_type=branch_type, task_id=task_id, base_branch=base_branch,
                   head_sha=head_sha, is_protected=is_protected, status="active",
                   created_by=created_by, created_at=datetime.now(timezone.utc),
                   merged_at=None, deleted_at=None)
        self.rows[row.id] = row
        return row

    async def get_by_name(self, db, repository_id, name):
        return next((r for r in self.rows.values()
                    if r.repository_id == repository_id and r.name == name), None)

    async def list_for_repository(self, db, repository_id, status=None):
        vals = [r for r in self.rows.values() if r.repository_id == repository_id]
        if status:
            vals = [r for r in vals if r.status == status]
        return vals

    async def update_head(self, db, branch_id, head_sha):
        if branch_id in self.rows:
            self.rows[branch_id].head_sha = head_sha

    async def mark_merged(self, db, branch_id):
        if branch_id in self.rows:
            self.rows[branch_id].status = "merged"

    async def mark_deleted(self, db, branch_id):
        if branch_id in self.rows:
            self.rows[branch_id].status = "deleted"


class FakePullRequestTable:
    def __init__(self):
        self.rows: Dict[str, SimpleNamespace] = {}

    async def create(self, db, repository_id, title, source_branch, target_branch="develop",
                     description=None, task_id=None, provider_pr_number=None,
                     reviewers=None, html_url=None, merge_strategy="squash"):
        row = _row(id=str(uuid.uuid4()), repository_id=repository_id, title=title,
                   source_branch=source_branch, target_branch=target_branch,
                   description=description, task_id=task_id,
                   provider_pr_number=provider_pr_number, reviewers=reviewers or [],
                   html_url=html_url, merge_strategy=merge_strategy, status="open",
                   merge_sha=None, opened_at=datetime.now(timezone.utc),
                   approved_at=None, merged_at=None, closed_at=None)
        self.rows[row.id] = row
        return row

    async def get_by_id(self, db, pull_request_id):
        return self.rows.get(pull_request_id)

    async def list_for_repository(self, db, repository_id, status=None):
        vals = [r for r in self.rows.values() if r.repository_id == repository_id]
        if status:
            vals = [r for r in vals if r.status == status]
        return vals

    async def mark_approved(self, db, pull_request_id):
        r = self.rows[pull_request_id]
        r.status = "approved"
        r.approved_at = datetime.now(timezone.utc)

    async def mark_merged(self, db, pull_request_id, merge_sha):
        r = self.rows[pull_request_id]
        r.status = "merged"
        r.merge_sha = merge_sha
        r.merged_at = datetime.now(timezone.utc)

    async def mark_closed(self, db, pull_request_id):
        self.rows[pull_request_id].status = "closed"

    async def mark_conflicted(self, db, pull_request_id):
        self.rows[pull_request_id].status = "conflicted"


class FakeEventTable:
    def __init__(self):
        self.rows: List[SimpleNamespace] = []

    async def record(self, db, event_type, repository_id=None, project_id=None,
                     entity_type=None, entity_id=None, actor="VISION Bot", payload=None):
        row = _row(id=str(uuid.uuid4()), repository_id=repository_id, project_id=project_id,
                   event_type=event_type, entity_type=entity_type, entity_id=entity_id,
                   actor=actor, payload=payload or {}, recorded_at=datetime.now(timezone.utc))
        self.rows.append(row)
        return row.id

    async def list_for_repository(self, db, repository_id, limit=100):
        return [r for r in self.rows if r.repository_id == repository_id][:limit]


class FakeProvider(BaseRepositoryProvider):
    """Configurable fake — set `.fail_with` to raise a given exception on any call."""

    name = "github"

    def __init__(self):
        self.fail_with: Optional[Exception] = None
        self.created_repos: Dict[str, ProviderRepoResult] = {}
        self.branches: Dict[str, str] = {}   # name -> head_sha
        self.commits: List[str] = []
        self.pull_requests: Dict[int, ProviderPullRequestResult] = {}
        self._pr_counter = 0
        self.merged_prs: List[int] = []
        self.releases: Dict[str, ProviderReleaseResult] = {}

    def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with

    async def create_repository(self, owner, name, description, visibility):
        self._maybe_fail()
        result = ProviderRepoResult(
            provider_repo_id=str(uuid.uuid4()), owner=owner, name=name,
            full_name=f"{owner}/{name}", default_branch="main",
            clone_url=f"https://github.com/{owner}/{name}.git",
            html_url=f"https://github.com/{owner}/{name}", visibility=visibility,
        )
        self.created_repos[name] = result
        self.branches["main"] = "sha-main-0"
        return result

    async def get_repository(self, owner, name):
        self._maybe_fail()
        return self.created_repos[name]

    async def create_branch(self, owner, repo, branch_name, base_branch):
        self._maybe_fail()
        base_sha = self.branches.get(base_branch, "sha-base-0")
        self.branches[branch_name] = base_sha
        return ProviderBranchResult(name=branch_name, head_sha=base_sha)

    async def get_branch(self, owner, repo, branch_name):
        self._maybe_fail()
        return ProviderBranchResult(name=branch_name, head_sha=self.branches[branch_name])

    async def delete_branch(self, owner, repo, branch_name):
        self._maybe_fail()
        self.branches.pop(branch_name, None)

    async def commit_files(self, owner, repo, branch_name, message, files,
                           author_name=None, author_email=None):
        self._maybe_fail()
        sha = f"sha-{len(self.commits) + 1}"
        self.commits.append(sha)
        self.branches[branch_name] = sha
        return ProviderCommitResult(sha=sha, html_url=f"https://github.com/{owner}/{repo}/commit/{sha}")

    async def create_pull_request(self, owner, repo, title, body, head, base, reviewers):
        self._maybe_fail()
        self._pr_counter += 1
        result = ProviderPullRequestResult(
            number=self._pr_counter,
            html_url=f"https://github.com/{owner}/{repo}/pull/{self._pr_counter}",
            state="open",
        )
        self.pull_requests[self._pr_counter] = result
        return result

    async def get_pull_request(self, owner, repo, number):
        self._maybe_fail()
        return self.pull_requests[number]

    async def merge_pull_request(self, owner, repo, number, commit_message):
        self._maybe_fail()
        self.merged_prs.append(number)
        return f"merge-sha-{number}"

    async def create_release(self, owner, repo, tag_name, target_commitish, name, body, prerelease):
        self._maybe_fail()
        if tag_name in self.releases:
            raise DuplicateTagError(f"Release tag '{tag_name}' already exists")
        result = ProviderReleaseResult(
            tag_name=tag_name, html_url=f"https://github.com/{owner}/{repo}/releases/{tag_name}",
            target_sha=target_commitish,
        )
        self.releases[tag_name] = result
        return result

    async def delete_release(self, owner, repo, tag_name):
        self._maybe_fail()
        self.releases.pop(tag_name, None)


def make_deps(provider: Optional[FakeProvider] = None) -> RepositoryDeps:
    return RepositoryDeps(
        db_factory=_fake_db_factory,
        provider=provider or FakeProvider(),
        nats=AsyncMock(),
        default_owner="vision-org",
    )


def patch_tables(monkeypatch):
    """Patches every manager module's imported repository classes with fakes."""
    repo_table = FakeRepositoryTable()
    branch_table = FakeBranchTable()
    pr_table = FakePullRequestTable()
    event_table = FakeEventTable()

    import infrastructure.database.repositories as repos_mod
    monkeypatch.setattr(repos_mod, "RepositoryRepository", repo_table, raising=False)
    monkeypatch.setattr(repos_mod, "BranchRepository", branch_table, raising=False)
    monkeypatch.setattr(repos_mod, "PullRequestRepository", pr_table, raising=False)
    monkeypatch.setattr(repos_mod, "RepositoryEventRepository", event_table, raising=False)

    import services.repository.managers.repository_manager as rm
    import services.repository.managers.branch_manager as bm
    import services.repository.managers.commit_manager as cm
    import services.repository.managers.pull_request_manager as pm
    import services.repository.managers.audit_manager as am

    monkeypatch.setattr(rm, "RepositoryRepository", repo_table)
    monkeypatch.setattr(bm, "RepositoryRepository", repo_table)
    monkeypatch.setattr(bm, "BranchRepository", branch_table)
    monkeypatch.setattr(cm, "RepositoryRepository", repo_table)
    monkeypatch.setattr(cm, "BranchRepository", branch_table)
    monkeypatch.setattr(pm, "RepositoryRepository", repo_table)
    monkeypatch.setattr(pm, "PullRequestRepository", pr_table)
    monkeypatch.setattr(am, "RepositoryEventRepository", event_table)

    import services.repository.managers.release_manager as relm
    monkeypatch.setattr(relm, "RepositoryRepository", repo_table)

    return SimpleNamespace(repo=repo_table, branch=branch_table, pr=pr_table, event=event_table)


@pytest.fixture
def tables(monkeypatch):
    return patch_tables(monkeypatch)


# ═══════════════════════════════════════════════════════════════
# LAYER 1 — Unit: naming policy, commit formatting, status mapping
# ═══════════════════════════════════════════════════════════════

class TestSlugify:
    def test_lowercases_and_hyphenates(self):
        assert slugify("My Cool Project!") == "my-cool-project"

    def test_collapses_repeated_separators(self):
        assert slugify("a---b   c") == "a-b-c"

    def test_empty_input_falls_back(self):
        assert slugify("###") == "repo"


class TestBranchNamingPolicy:
    def test_feature_branch_name(self):
        assert build_branch_name("feature", "TASK-1", None, "add-login") == "feature/TASK-1-add-login"

    def test_fix_branch_name(self):
        assert build_branch_name("fix", "TASK-2", None, "null-pointer") == "fix/TASK-2-null-pointer"

    def test_hotfix_branch_name(self):
        assert build_branch_name("hotfix", None, "INC-9", None) == "hotfix/INC-9"

    def test_feature_without_slug_raises(self):
        with pytest.raises(InvalidBranchNameError):
            build_branch_name("feature", "TASK-1", None, None)

    def test_feature_without_task_id_raises(self):
        with pytest.raises(InvalidBranchNameError):
            build_branch_name("feature", None, None, "add-login")

    def test_hotfix_without_incident_id_raises(self):
        with pytest.raises(InvalidBranchNameError):
            build_branch_name("hotfix", None, None, None)

    def test_invalid_slug_characters_rejected(self):
        with pytest.raises(InvalidBranchNameError):
            build_branch_name("feature", "TASK-1", None, "Add Login!!")

    def test_unsupported_branch_type_rejected(self):
        with pytest.raises(InvalidBranchNameError):
            build_branch_name("release", "TASK-1", None, "x")


class TestProtectedBranchGuard:
    def test_main_is_protected(self):
        with pytest.raises(ProtectedBranchViolationError):
            assert_not_protected("main", action="delete")

    def test_develop_is_protected(self):
        with pytest.raises(ProtectedBranchViolationError):
            assert_not_protected("develop", action="commit directly to")

    def test_feature_branch_is_not_protected(self):
        assert_not_protected("feature/TASK-1-x", action="delete")  # no raise


class TestCommitFormatting:
    def test_message_includes_metadata_trailer(self):
        meta = CommitMetadata(project_id="p1", workflow_id="w1", task_id="t1", agent_id="a1")
        msg = format_commit_message("feat: add login", meta)
        assert msg.startswith("feat: add login")
        assert "Project-Id: p1" in msg
        assert "Workflow-Id: w1" in msg
        assert "Task-Id: t1" in msg
        assert "Agent-Id: a1" in msg

    def test_lead_id_included_when_present(self):
        meta = CommitMetadata(project_id="p1", workflow_id="w1", task_id="t1",
                              agent_id="a1", lead_id="lead-1")
        msg = format_commit_message("feat: x", meta)
        assert "Lead-Id: lead-1" in msg

    def test_lead_id_omitted_when_absent(self):
        meta = CommitMetadata(project_id="p1", workflow_id="w1", task_id="t1", agent_id="a1")
        msg = format_commit_message("feat: x", meta)
        assert "Lead-Id" not in msg


class TestGitHubProviderStatusMapping:
    def _resp(self, status_code, body="{}"):
        return httpx.Response(status_code, text=body, request=httpx.Request("GET", "https://x"))

    def test_401_maps_to_permission_denied(self):
        with pytest.raises(PermissionDeniedError):
            GitHubProvider._raise_for_status(self._resp(401), "ctx")

    def test_403_maps_to_permission_denied(self):
        with pytest.raises(PermissionDeniedError):
            GitHubProvider._raise_for_status(self._resp(403), "ctx")

    def test_409_maps_to_merge_conflict(self):
        with pytest.raises(MergeConflictError):
            GitHubProvider._raise_for_status(self._resp(409), "ctx")

    def test_422_already_exists_maps_to_duplicate_tag(self):
        body = '{"message": "Validation failed: tag already exists"}'
        with pytest.raises(DuplicateTagError):
            GitHubProvider._raise_for_status(self._resp(422, body), "ctx")

    def test_503_maps_to_provider_unavailable(self):
        with pytest.raises(ProviderUnavailableError):
            GitHubProvider._raise_for_status(self._resp(503), "ctx")

    def test_2xx_does_not_raise(self):
        GitHubProvider._raise_for_status(self._resp(200), "ctx")   # no raise


# ═══════════════════════════════════════════════════════════════
# LAYER 2 — Graph routing tests (no provider/DB)
# ═══════════════════════════════════════════════════════════════

class TestRepositoryGraphRouting:
    def _base_state(self):
        return {"phase_status": "running", "retry_count": 0, "max_retries": 3,
                "retryable": True, "create_release": False}

    def test_validate_success_routes_to_create_branch(self):
        s = self._base_state()
        assert route_after_validate(s) == "create_branch"

    def test_create_branch_success_routes_to_commit(self):
        s = self._base_state()
        assert route_after_create_branch(s) == "commit"

    def test_commit_success_routes_to_open_pr(self):
        s = self._base_state()
        assert route_after_commit(s) == "open_pr"

    def test_open_pr_success_routes_to_approval(self):
        s = self._base_state()
        assert route_after_open_pr(s) == "approval"

    def test_transient_failure_under_max_retries_routes_to_retry(self):
        s = self._base_state()
        s["phase_status"] = "failed"; s["retry_count"] = 1
        assert route_after_validate(s) == "retry"

    def test_transient_failure_at_max_retries_routes_to_escalate(self):
        s = self._base_state()
        s["phase_status"] = "failed"; s["retry_count"] = 3
        assert route_after_validate(s) == "escalate"

    def test_non_retryable_failure_routes_to_dead_letter(self):
        s = self._base_state()
        s["phase_status"] = "failed"; s["retryable"] = False; s["retry_count"] = 0
        assert route_after_validate(s) == "dead_letter"

    def test_approval_gate_approved(self):
        s = self._base_state(); s["approval_status"] = "approved"
        assert route_approval_gate(s) == "approved"

    def test_approval_gate_rejected(self):
        s = self._base_state(); s["approval_status"] = "rejected"
        assert route_approval_gate(s) == "rejected"

    def test_approval_gate_pending(self):
        s = self._base_state(); s["approval_status"] = None
        assert route_approval_gate(s) == "pending"

    def test_merge_success_without_release_routes_to_publish(self):
        s = self._base_state(); s["create_release"] = False
        assert route_after_merge(s) == "publish_events"

    def test_merge_success_with_release_routes_to_release(self):
        s = self._base_state(); s["create_release"] = True
        assert route_after_merge(s) == "release"


# ═══════════════════════════════════════════════════════════════
# LAYER 3 — Integration: managers against fake provider + fake DB
# ═══════════════════════════════════════════════════════════════

class TestRepositoryManagerIntegration:
    async def test_create_repository_happy_path(self, tables):
        deps = make_deps()
        mgr = RepositoryManager(deps)
        req = CreateRepositoryRequest(project_id="proj-1", project_name="Cool App")

        result = await mgr.create_repository(req)

        assert result.project_id == "proj-1"
        assert result.name == "cool-app"
        assert result.default_branch == "main"
        assert "develop" in deps.provider.branches
        assert len(deps.provider.commits) == 1                 # scaffold commit
        assert any(e.event_type == "repository.created" for e in tables.event.rows)

    async def test_create_repository_is_idempotent(self, tables):
        deps = make_deps()
        mgr = RepositoryManager(deps)
        req = CreateRepositoryRequest(project_id="proj-1", project_name="Cool App")

        first = await mgr.create_repository(req)
        second = await mgr.create_repository(req)

        assert first.id == second.id
        assert len(deps.provider.created_repos) == 1

    async def test_create_repository_without_owner_raises(self, tables):
        deps = RepositoryDeps(db_factory=_fake_db_factory, provider=FakeProvider(),
                              nats=AsyncMock(), default_owner=None)
        mgr = RepositoryManager(deps)
        req = CreateRepositoryRequest(project_id="proj-x", project_name="X")
        with pytest.raises(RepositoryServiceError):
            await mgr.create_repository(req)


class TestBranchManagerIntegration:
    async def _seeded_repo(self, tables, deps, project_id="proj-1"):
        return await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id=project_id, project_name="App")
        )

    async def test_create_feature_branch(self, tables):
        deps = make_deps()
        await self._seeded_repo(tables, deps)
        mgr = BranchManager(deps)

        branch = await mgr.create_branch(CreateBranchRequest(
            project_id="proj-1", branch_type=BranchType.FEATURE,
            task_id="TASK-1", slug="add-login",
        ))

        assert branch.name == "feature/TASK-1-add-login"
        assert branch.base_branch == "develop"

    async def test_hotfix_branch_bases_off_default_branch(self, tables):
        deps = make_deps()
        await self._seeded_repo(tables, deps)
        mgr = BranchManager(deps)

        branch = await mgr.create_branch(CreateBranchRequest(
            project_id="proj-1", branch_type=BranchType.HOTFIX, incident_id="INC-9",
        ))

        assert branch.name == "hotfix/INC-9"
        assert branch.base_branch == "main"

    async def test_create_branch_without_repository_raises(self, tables):
        deps = make_deps()
        mgr = BranchManager(deps)
        with pytest.raises(RepositoryServiceError):
            await mgr.create_branch(CreateBranchRequest(
                project_id="no-such-project", branch_type=BranchType.FEATURE,
                task_id="TASK-1", slug="x",
            ))

    async def test_delete_protected_branch_raises(self, tables):
        deps = make_deps()
        await self._seeded_repo(tables, deps)
        mgr = BranchManager(deps)
        with pytest.raises(ProtectedBranchViolationError):
            await mgr.delete_branch("proj-1", "develop")


class TestCommitManagerIntegration:
    async def test_commit_to_feature_branch_succeeds(self, tables):
        deps = make_deps()
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )
        branch = await BranchManager(deps).create_branch(CreateBranchRequest(
            project_id="proj-1", branch_type=BranchType.FEATURE,
            task_id="TASK-1", slug="add-login",
        ))

        commit = await CommitManager(deps).commit_files(CommitFilesRequest(
            project_id="proj-1", branch_name=branch.name, message="feat: add login",
            files=[FileChange(path="backend/auth.py", content="# auth")],
            metadata=CommitMetadata(project_id="proj-1", workflow_id="w1",
                                    task_id="TASK-1", agent_id="engineer-1"),
        ))

        assert commit.branch_name == branch.name
        assert "Task-Id: TASK-1" in commit.message

    async def test_direct_commit_to_main_refused(self, tables):
        deps = make_deps()
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )
        with pytest.raises(ProtectedBranchViolationError):
            await CommitManager(deps).commit_files(CommitFilesRequest(
                project_id="proj-1", branch_name="main", message="sneaky",
                files=[FileChange(path="x.py", content="x")],
                metadata=CommitMetadata(project_id="proj-1", workflow_id="w1",
                                        task_id="t1", agent_id="a1"),
            ))

    async def test_direct_commit_to_develop_refused(self, tables):
        deps = make_deps()
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )
        with pytest.raises(ProtectedBranchViolationError):
            await CommitManager(deps).commit_files(CommitFilesRequest(
                project_id="proj-1", branch_name="develop", message="sneaky",
                files=[FileChange(path="x.py", content="x")],
                metadata=CommitMetadata(project_id="proj-1", workflow_id="w1",
                                        task_id="t1", agent_id="a1"),
            ))


class TestPullRequestManagerIntegration:
    async def _setup_pr(self, tables, deps):
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )
        branch = await BranchManager(deps).create_branch(CreateBranchRequest(
            project_id="proj-1", branch_type=BranchType.FEATURE,
            task_id="TASK-1", slug="add-login",
        ))
        pr = await PullRequestManager(deps).create_pull_request(CreatePullRequestRequest(
            project_id="proj-1", source_branch=branch.name, title="Add login",
        ))
        return pr

    async def test_create_pull_request_defaults_to_develop(self, tables):
        deps = make_deps()
        pr = await self._setup_pr(tables, deps)
        assert pr.target_branch == "develop"
        assert pr.merge_strategy == "squash"
        assert pr.status == "open"

    async def test_merge_without_approval_raises(self, tables):
        deps = make_deps()
        pr = await self._setup_pr(tables, deps)
        with pytest.raises(PermissionDeniedError):
            await PullRequestManager(deps).merge_pull_request(
                MergePullRequestRequest(project_id="proj-1", pull_request_id=pr.id)
            )

    async def test_approve_then_merge_happy_path(self, tables):
        deps = make_deps()
        pr = await self._setup_pr(tables, deps)
        pr_mgr = PullRequestManager(deps)

        approved = await pr_mgr.approve_pull_request(ApprovePullRequestRequest(
            project_id="proj-1", pull_request_id=pr.id, approved_by="lead-1",
        ))
        assert approved.status == "approved"

        merged = await pr_mgr.merge_pull_request(
            MergePullRequestRequest(project_id="proj-1", pull_request_id=pr.id)
        )
        assert merged.status == "merged"
        assert merged.merge_sha is not None

    async def test_merge_conflict_marks_pr_conflicted(self, tables):
        deps = make_deps()
        pr = await self._setup_pr(tables, deps)
        pr_mgr = PullRequestManager(deps)
        await pr_mgr.approve_pull_request(ApprovePullRequestRequest(
            project_id="proj-1", pull_request_id=pr.id, approved_by="lead-1",
        ))

        deps.provider.fail_with = MergeConflictError("conflict!")
        with pytest.raises(MergeConflictError):
            await pr_mgr.merge_pull_request(
                MergePullRequestRequest(project_id="proj-1", pull_request_id=pr.id)
            )

        stored = tables.pr.rows[pr.id]
        assert stored.status == "conflicted"


class TestReleaseManagerIntegration:
    async def _seeded_repo(self, deps):
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )

    async def test_create_release_happy_path(self, tables):
        deps = make_deps()
        await self._seeded_repo(deps)
        release = await ReleaseManager(deps).create_release(
            CreateReleaseRequest(project_id="proj-1", tag_name="v1.0.0")
        )
        assert release.tag_name == "v1.0.0"
        assert any(e.event_type == "release.created" for e in tables.event.rows)

    async def test_duplicate_tag_raises(self, tables):
        deps = make_deps()
        await self._seeded_repo(deps)
        rel_mgr = ReleaseManager(deps)
        await rel_mgr.create_release(CreateReleaseRequest(project_id="proj-1", tag_name="v1.0.0"))
        with pytest.raises(DuplicateTagError):
            await rel_mgr.create_release(CreateReleaseRequest(project_id="proj-1", tag_name="v1.0.0"))

    async def test_rollback_release_records_reason(self, tables):
        deps = make_deps()
        await self._seeded_repo(deps)
        rel_mgr = ReleaseManager(deps)
        await rel_mgr.create_release(CreateReleaseRequest(project_id="proj-1", tag_name="v1.0.0"))

        await rel_mgr.rollback_release(RollbackReleaseRequest(
            project_id="proj-1", tag_name="v1.0.0", reason="critical regression",
        ))

        rollback_events = [e for e in tables.event.rows if e.event_type == "release.rollback"]
        assert len(rollback_events) == 1
        assert rollback_events[0].payload["reason"] == "critical regression"


# ═══════════════════════════════════════════════════════════════
# LAYER 4 — Failure tests
# ═══════════════════════════════════════════════════════════════

class TestFailureModes:
    async def test_github_unavailable_on_create_repository(self, tables):
        provider = FakeProvider()
        provider.fail_with = ProviderUnavailableError("GitHub is down")
        deps = make_deps(provider)
        with pytest.raises(ProviderUnavailableError):
            await RepositoryManager(deps).create_repository(
                CreateRepositoryRequest(project_id="proj-1", project_name="App")
            )

    async def test_github_unavailable_on_commit(self, tables):
        deps = make_deps()
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )
        branch = await BranchManager(deps).create_branch(CreateBranchRequest(
            project_id="proj-1", branch_type=BranchType.FEATURE,
            task_id="TASK-1", slug="x",
        ))
        deps.provider.fail_with = ProviderUnavailableError("timeout")
        with pytest.raises(ProviderUnavailableError):
            await CommitManager(deps).commit_files(CommitFilesRequest(
                project_id="proj-1", branch_name=branch.name, message="x",
                files=[FileChange(path="a.py", content="a")],
                metadata=CommitMetadata(project_id="proj-1", workflow_id="w1",
                                        task_id="t1", agent_id="a1"),
            ))

    async def test_duplicate_tag_on_release(self, tables):
        deps = make_deps()
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )
        rel_mgr = ReleaseManager(deps)
        await rel_mgr.create_release(CreateReleaseRequest(project_id="proj-1", tag_name="v9"))
        with pytest.raises(DuplicateTagError):
            await rel_mgr.create_release(CreateReleaseRequest(project_id="proj-1", tag_name="v9"))

    async def test_invalid_branch_name_missing_slug(self, tables):
        deps = make_deps()
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )
        with pytest.raises(InvalidBranchNameError):
            await BranchManager(deps).create_branch(CreateBranchRequest(
                project_id="proj-1", branch_type=BranchType.FIX, task_id="TASK-1", slug=None,
            ))

    async def test_permission_failure_deleting_protected_branch(self, tables):
        deps = make_deps()
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )
        with pytest.raises(ProtectedBranchViolationError):
            await BranchManager(deps).delete_branch("proj-1", "main")

    async def test_merge_bypassing_approval_denied(self, tables):
        deps = make_deps()
        await RepositoryManager(deps).create_repository(
            CreateRepositoryRequest(project_id="proj-1", project_name="App")
        )
        branch = await BranchManager(deps).create_branch(CreateBranchRequest(
            project_id="proj-1", branch_type=BranchType.FEATURE,
            task_id="TASK-1", slug="x",
        ))
        pr = await PullRequestManager(deps).create_pull_request(CreatePullRequestRequest(
            project_id="proj-1", source_branch=branch.name, title="x",
        ))
        with pytest.raises(PermissionDeniedError):
            await PullRequestManager(deps).merge_pull_request(
                MergePullRequestRequest(project_id="proj-1", pull_request_id=pr.id)
            )

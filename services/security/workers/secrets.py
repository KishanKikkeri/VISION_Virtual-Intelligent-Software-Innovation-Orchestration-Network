"""services/security/workers/secrets.py — Secret Scanner Worker."""
from __future__ import annotations

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.security.models import SecretHit, SecretScan
from services.security.utils import scan_content_for_secrets


@AgentFactory.register("secret_scanner_worker")
class SecretScannerWorker(BaseAgent):
    """
    Scans every file in Engineering's source_code artifact for
    accidentally committed secrets. Deterministic regex-based scan —
    no LLM call needed, since precise pattern matching is more reliable
    than LLM judgement for this class of check (and keeps the Hard Fail
    gate fully exercisable without a live LLM in this environment).
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        source = task.context.get_artifact("source_code", {})
        files = source.get("files", []) if isinstance(source, dict) else []

        hits = []
        for f in files:
            path = f.get("path", "unknown") if isinstance(f, dict) else "unknown"
            content = f.get("content", "") if isinstance(f, dict) else ""
            for hit in scan_content_for_secrets(path, content):
                hits.append(SecretHit(file=hit["file"], rule=hit["rule"], line=hit["line"]))

        scan = SecretScan(project_id=task.project_id, files_scanned=len(files), secrets=hits)

        artifact = await self.create_artifact(
            task, "secret_scan", {**scan.model_dump(), "project_id": task.project_id},
        )
        status = TaskStatus.FAILED if scan.has_secrets else TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.task_id, agent_id=self.agent_id, status=status,
            content=scan.model_dump(),
            summary=f"Secret scan: {len(files)} file(s) scanned, {scan.secret_count} secret(s) found",
            quality_score=0.95 if not scan.has_secrets else 0.0,
            artifacts=[artifact],
            failure_reason=None if not scan.has_secrets
            else f"{scan.secret_count} potential secret(s) detected — hard fail per spec",
        )

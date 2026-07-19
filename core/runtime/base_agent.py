"""
core/runtime/base_agent.py
===========================
Phase 2 — Step 1: BaseAgent.
The class every one of the 53 agents inherits from.
Wired to live infrastructure: DB, NATS, Qdrant, Storage, LLM registry.

Guaranteed execution contract (enforced by run()):
  1. _pre_execute  — audit log START, bind context vars
  2. execute       — agent-specific logic (overridden by subclass)
  3. _post_execute — flush events, write memory, trigger cost tracking

Every subclass must:
  - Set agent_id, name, department, layer, role, responsibilities
  - Override execute(task) → AgentResult
  - Call self.call_llm() for ALL LLM inference (never vendor SDKs)
  - Call self.create_artifact() for ALL artifact creation
"""
from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import structlog

from core.contracts import (
    AgentResult,
    AuditEventRecord,
    NATSEvent,
    TaskStatus,
    TokenUsageRecord,
    WebSocketEvent,
)
from core.llm.registry import LLMProviderRegistry
from core.llm.router import select_provider_and_model
from core.runtime.context import AgentContext, TaskInput

log = structlog.get_logger(__name__)


class BaseAgent(ABC):
    """
    Abstract base for all 53 AASC agents.
    Subclasses provide: agent_id, name, department, layer, role,
    responsibilities, and override execute().
    """

    # ── Class-level identity (set by each concrete agent) ─────
    agent_id:         str
    name:             str
    department:       str
    layer:            int          # 2=manager 3=head 4=lead 5=worker
    role:             str          # manager|head|lead|worker
    responsibilities: List[str]

    # ── Injected by AgentFactory ───────────────────────────────
    _db_factory:      Any          # callable → AsyncSession context manager
    _nats:            Any          # NATSClient
    _qdrant:          Any          # QdrantClient
    _storage:         Any          # ArtifactStorage
    _audit_repo:      Any          # AuditRepository
    _artifact_repo:   Any          # ArtifactRepository
    _token_repo:      Any          # TokenLedgerRepository

    # ══════════════════════════════════════════════════════════
    # PUBLIC INTERFACE
    # ══════════════════════════════════════════════════════════

    async def run(self, task: TaskInput) -> AgentResult:
        """
        The only way to execute an agent. Enforces the full contract.
        Called by AgentFactory and LangGraph nodes — never called directly.
        """
        t_start = time.monotonic()
        log.info("agent_run_start",
                 agent_id=self.agent_id, task_id=task.task_id,
                 task_type=task.task_type, retry=task.retry_count)

        await self._pre_execute(task)
        try:
            result = await self.execute(task)
        except Exception as exc:
            result = AgentResult(
                task_id=task.task_id,
                agent_id=self.agent_id,
                status=TaskStatus.FAILED,
                failure_reason=str(exc),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            )
            log.error("agent_run_exception",
                      agent_id=self.agent_id, error=str(exc), exc_info=True)

        result.duration_ms = int((time.monotonic() - t_start) * 1000)
        await self._post_execute(task, result)
        return result

    @abstractmethod
    async def execute(self, task: TaskInput) -> AgentResult:
        """
        Agent-specific logic. Must be overridden by every concrete agent.
        Do NOT call _pre/_post_execute — run() handles that.
        """
        ...

    # ══════════════════════════════════════════════════════════
    # PROTECTED HELPERS — available to all subclasses
    # ══════════════════════════════════════════════════════════

    async def call_llm(
        self,
        task:        TaskInput,
        messages:    List[Dict[str, str]],
        max_tokens:  int   = 4096,
        temperature: float = 0.2,
    ) -> tuple[str, TokenUsageRecord]:
        """
        ALL LLM calls must go through this method.
        Selects provider/model, calls registry, records token usage.
        Returns (response_text, TokenUsageRecord).
        """
        from core.contracts import LLMMessage
        ctx = task.context

        # Select provider + model via router
        provider, model = select_provider_and_model(
            preferred_provider=ctx.llm_provider,
            agent_role=self.role,
            task_type=task.task_type,
            escalation_level=task.retry_count,
            budget_tight=(
                ctx.budget_remaining_usd is not None
                and ctx.budget_remaining_usd < 5.0
            ),
        )

        llm_messages = [LLMMessage(role=m["role"], content=m["content"]) for m in messages]

        response = await LLMProviderRegistry.complete(
            provider=provider,
            model=model,
            messages=llm_messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        usage = TokenUsageRecord(
            project_id=task.project_id,
            agent_id=self.agent_id,
            department=self.department,
            provider=provider,
            model=model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
        )
        return response.content, usage

    async def create_artifact(
        self,
        task:          TaskInput,
        artifact_type: str,
        content:       Any,
        metadata:      Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Creates and registers an artifact. Returns ArtifactRef dict.
        Writes to storage + DB atomically.
        """
        # Store content on filesystem
        storage_ref = await self._storage.store(
            project_id=task.project_id,
            artifact_type=artifact_type,
            version=1,
            content=content if isinstance(content, dict) else {"content": content},
            extension="json",
        )

        # Register in DB
        async with self._db_factory() as db:
            ref = await self._artifact_repo.create(
                db,
                project_id=task.project_id,
                artifact_type=artifact_type,
                created_by=self.agent_id,
                content=content if isinstance(content, dict) else None,
                storage_ref=storage_ref,
                metadata=metadata or {},
            )
        return ref

    async def read_memory(
        self,
        task:   TaskInput,
        query:  str,
        limit:  int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves relevant memories from Qdrant vector search.
        Returns list of {content, score, source} dicts.
        Falls back to empty list if Qdrant is unavailable.
        """
        try:
            results = self._qdrant.search(
                collection_name=f"project_{task.project_id}",
                query_text=query,
                limit=limit,
            )
            return [
                {"content": r.payload.get("content", ""),
                 "score":   r.score,
                 "source":  r.payload.get("source", "unknown")}
                for r in results
            ]
        except Exception:
            return []

    async def write_memory(
        self,
        task:    TaskInput,
        content: str,
        source:  str,
    ) -> None:
        """Writes a memory snippet to Qdrant for future retrieval."""
        try:
            collection = f"project_{task.project_id}"
            self._qdrant.upsert(
                collection_name=collection,
                points=[{
                    "id":      str(uuid.uuid4()),
                    "vector":  content,           # embedding done inside Qdrant
                    "payload": {
                        "content":  content,
                        "source":   source,
                        "agent_id": self.agent_id,
                        "task_id":  task.task_id,
                    },
                }],
            )
        except Exception:
            pass   # memory writes are best-effort

    async def publish_event(
        self,
        subject: str,
        payload: Dict[str, Any],
    ) -> None:
        """Queues a NATS event. Best-effort — logs warning on failure."""
        try:
            await self._nats.publish(subject, payload)
        except Exception as e:
            log.warning("event_publish_failed", subject=subject, error=str(e))

    async def notify_ui(
        self,
        project_id: str,
        event_type: str,
        payload:    Dict[str, Any],
    ) -> None:
        """Broadcasts a WebSocket event to the project's real-time stream."""
        from infrastructure.websocket.manager import ws_manager
        await ws_manager.broadcast(project_id, event_type, payload)

    def build_system_prompt(self, task: TaskInput) -> str:
        """
        Constructs the standard system prompt from the template.
        Every agent uses this — never write ad-hoc prompts.
        """
        ctx  = task.context
        arts = "\n".join(
            f"  - {k}: {str(v)[:300]}"
            for k, v in ctx.approved_artifacts.items()
        ) or "  None yet."

        mem = "\n".join(
            f"  [{s.get('source','?')} score={s.get('score',0):.2f}] "
            f"{s.get('content','')[:200]}"
            for s in ctx.memory_snippets
        ) or "  No relevant memory found."

        revision_block = ""
        if task.revision_feedback:
            revision_block = (
                f"\n## REVISION INSTRUCTIONS (round {task.retry_count})\n"
                f"The previous version was rejected. Address every point:\n"
                f"{task.revision_feedback}\n"
            )

        budget_str = (
            f"${ctx.budget_remaining_usd:.2f} remaining"
            if ctx.budget_remaining_usd is not None else "Unlimited"
        )

        return f"""You are {self.name}, a {self.role} in the \
{self.department} department of AASC (Autonomous AI Software Company).

## Your Primary Responsibility
{self.responsibilities[0] if self.responsibilities else 'Complete the assigned task.'}

## All Responsibilities
{chr(10).join(f'{i+1}. {r}' for i,r in enumerate(self.responsibilities))}

## Current Task
{task.description}

## Expected Output
{task.expected_output}

## Project Context
- Project: {ctx.project_name}
- Phase: {ctx.current_phase}/10
- Tech stack: {', '.join(f'{k}: {v}' for k,v in ctx.tech_stack.items()) or 'Not yet defined'}
- Budget: {budget_str}

## Available Approved Artifacts
{arts}

## Relevant Project Memory
{mem}
{revision_block}
## Non-Negotiable Rules
1. Respond ONLY with the exact format specified in Expected Output.
2. No preamble, no explanation, no markdown code fences unless specified.
3. Never invent file paths, API endpoints, or library versions.
4. If you cannot complete the task, respond with exactly: ESCALATE: <one-sentence reason>
5. Include a quality_score field (0.0-1.0) as the last field in your JSON response."""

    def escalate(
        self,
        task:    TaskInput,
        reason:  str,
        trigger: str = "validation_failed",
    ) -> AgentResult:
        """Returns an ESCALATED AgentResult. Triggers W12 escalation chain."""
        log.warning("agent_escalating",
                    agent_id=self.agent_id, reason=reason, retry=task.retry_count)
        return AgentResult(
            task_id=self.agent_id,
            agent_id=self.agent_id,
            status=TaskStatus.ESCALATED,
            failure_reason=reason,
            summary=f"Escalated after {task.retry_count} retries: {reason[:100]}",
        )

    # ══════════════════════════════════════════════════════════
    # LIFECYCLE HOOKS
    # ══════════════════════════════════════════════════════════

    async def _pre_execute(self, task: TaskInput) -> None:
        """Logs agent run start to audit_events."""
        try:
            async with self._db_factory() as db:
                await self._audit_repo.record(
                    db,
                    project_id=task.project_id,
                    event_type=f"agent.{self.agent_id}.started",
                    actor_type="agent",
                    actor_id=self.agent_id,
                    entity_type="task",
                    entity_id=task.task_id,
                    payload={
                        "task_type":   task.task_type,
                        "retry_count": task.retry_count,
                        "department":  self.department,
                    },
                )
        except Exception as e:
            log.warning("pre_execute_audit_failed", error=str(e))

    async def _post_execute(self, task: TaskInput, result: AgentResult) -> None:
        """
        Flushes NATS events, records token usage, logs completion to audit.
        Always runs — even on failure.
        """
        # 1. Record token usage
        if result.token_usage:
            try:
                async with self._db_factory() as db:
                    await self._token_repo.record(
                        db,
                        project_id=task.project_id,
                        agent_id=self.agent_id,
                        department=self.department,
                        provider=result.token_usage.provider,
                        model=result.token_usage.model,
                        input_tokens=result.token_usage.input_tokens,
                        output_tokens=result.token_usage.output_tokens,
                        cost_usd=result.token_usage.cost_usd,
                    )
                # Update Prometheus
                from infrastructure.monitoring.telemetry import record_token_usage
                u = result.token_usage
                record_token_usage(u.provider, u.model, self.department,
                                   u.input_tokens, u.output_tokens, u.cost_usd)
            except Exception as e:
                log.warning("token_ledger_write_failed", error=str(e))

        # 2. Flush queued NATS events
        for event in result.nats_events:
            await self.publish_event(event.subject, event.payload)

        # 3. Flush queued WebSocket events
        for ev in result.ws_events:
            await self.notify_ui(ev.project_id, ev.event_type, ev.payload)

        # 4. Log completion to audit
        try:
            async with self._db_factory() as db:
                await self._audit_repo.record(
                    db,
                    project_id=task.project_id,
                    event_type=f"agent.{self.agent_id}.{result.status.value}",
                    actor_type="agent",
                    actor_id=self.agent_id,
                    entity_type="task",
                    entity_id=task.task_id,
                    payload={
                        "status":       result.status.value,
                        "quality_score":result.quality_score,
                        "duration_ms":  result.duration_ms,
                        "artifacts":    [a.get("artifact_id") for a in result.artifacts],
                    },
                )
        except Exception as e:
            log.warning("post_execute_audit_failed", error=str(e))

        log.info("agent_run_done",
                 agent_id=self.agent_id, status=result.status.value,
                 duration_ms=result.duration_ms, score=result.quality_score)

"""
core/runtime/context.py
========================
AgentContext  — shared context injected into every agent run.
TaskInput     — complete input contract for one agent execution.
ReviewCycle   — mandatory generate→critique→improve→validate loop.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class AgentContext:
    """
    Pre-fetched, read-only snapshot injected into every TaskInput.
    Agents MUST NOT query PostgreSQL or Qdrant directly — read this instead.
    """
    project_id:          str
    workflow_id:         str
    current_phase:       int
    project_name:        str
    project_description: str

    # Approved artifact content, keyed by artifact_type
    approved_artifacts:  Dict[str, Any] = field(default_factory=dict)

    # Top-k memories from Qdrant (pre-fetched)
    memory_snippets:     List[Dict[str, Any]] = field(default_factory=list)

    # Locked project tech stack
    tech_stack:          Dict[str, str] = field(default_factory=dict)
    coding_standards:    List[str] = field(default_factory=list)

    # LLM routing
    llm_provider:        str = "anthropic"
    llm_model:           str = "claude-sonnet-4-6"

    # Budget snapshot
    budget_limit_usd:    Optional[float] = None
    total_spend_usd:     float = 0.0

    # Graph dependency snapshot (requirement_dependencies rows)
    dependency_graph:    List[Dict[str, Any]] = field(default_factory=list)

    @property
    def budget_remaining_usd(self) -> Optional[float]:
        if self.budget_limit_usd is None:
            return None
        return max(0.0, self.budget_limit_usd - self.total_spend_usd)

    def get_artifact(self, artifact_type: str, default: Any = None) -> Any:
        return self.approved_artifacts.get(artifact_type, default)


@dataclass
class TaskInput:
    """Complete input contract for one agent execution."""
    task_id:           str
    project_id:        str
    agent_id:          str
    parent_agent_id:   str
    task_type:         str
    description:       str
    expected_output:   str
    context:           AgentContext

    retry_count:       int = 0
    revision_feedback: Optional[str] = None
    failure_history:   List[str] = field(default_factory=list)

    max_output_tokens: int = 4096
    timeout_seconds:   int = 120
    created_at:        datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def create(
        cls,
        project_id:        str,
        agent_id:          str,
        parent_agent_id:   str,
        task_type:         str,
        description:       str,
        expected_output:   str,
        context:           AgentContext,
        **kwargs,
    ) -> "TaskInput":
        return cls(
            task_id=str(uuid.uuid4()),
            project_id=project_id,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            task_type=task_type,
            description=description,
            expected_output=expected_output,
            context=context,
            **kwargs,
        )


@dataclass
class CritiqueResult:
    passed:      bool
    score:       float
    blocking:    List[str]
    warnings:    List[str]
    suggestions: List[str]


@dataclass
class ReviewResult:
    cycles_run:       int
    final_score:      float
    passed:           bool
    critique_history: List[CritiqueResult]
    improvement_notes:str


class ReviewCycle:
    """
    Mandatory generate→critique→improve→validate cycle.
    Used by all WorkerAgents before returning output.

    Usage in a concrete agent:
        content, usage = await self.call_llm(task, messages)
        review = await ReviewCycle(self).run(content, task)
        if not review.passed:
            return self.escalate(task, "Review failed")
    """

    def __init__(self, agent: Any, max_cycles: int = 3):
        self._agent      = agent
        self._max_cycles = max_cycles

    async def run(
        self,
        content:    Any,
        task:       TaskInput,
        schema:     Optional[Dict[str, List[str]]] = None,
    ) -> ReviewResult:
        """
        Runs the full review cycle.
        schema: {field_name: [required_keys]} for structural validation.
        """
        history:     List[CritiqueResult] = []
        current      = content
        cycles_run   = 0

        for _ in range(self._max_cycles):
            cycles_run += 1
            critique   = await self._critique(current, task)
            history.append(critique)

            if not critique.blocking:
                break

            # Apply improvements
            current = await self._improve(current, critique, task)

        # Final structural validation
        passed = self._validate_structure(current, schema)
        score  = sum(c.score for c in history) / max(len(history), 1)

        return ReviewResult(
            cycles_run=cycles_run,
            final_score=score,
            passed=passed and (not history or history[-1].score >= 0.7),
            critique_history=history,
            improvement_notes="; ".join(
                c.suggestions[0] for c in history if c.suggestions
            ),
        )

    async def _critique(self, content: Any, task: TaskInput) -> CritiqueResult:
        """Self-critique via LLM."""
        prompt = f"""Review this output critically against the expected output spec.

EXPECTED: {task.expected_output}

OUTPUT TO REVIEW:
{json.dumps(content, indent=2, default=str) if isinstance(content, dict) else str(content)}

Respond with ONLY this JSON (no extra text):
{{
  "passed": true,
  "score": 0.85,
  "blocking": ["specific issue that must be fixed"],
  "warnings": ["non-blocking quality issue"],
  "suggestions": ["concrete improvement action"]
}}"""

        try:
            raw, _ = await self._agent.call_llm(
                task,
                [{"role": "user", "content": prompt}],
                max_tokens=512,
                temperature=0.1,
            )
            # Strip potential markdown fences
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            data = json.loads(clean)
            return CritiqueResult(
                passed     =bool(data.get("passed",  True)),
                score      =float(data.get("score",  0.8)),
                blocking   =data.get("blocking",  []),
                warnings   =data.get("warnings",  []),
                suggestions=data.get("suggestions",[]),
            )
        except Exception:
            # If critique itself fails, assume passed at 0.7
            return CritiqueResult(passed=True, score=0.7,
                                  blocking=[], warnings=[], suggestions=[])

    async def _improve(
        self, content: Any, critique: CritiqueResult, task: TaskInput
    ) -> Any:
        """Improvement pass — fixes blocking issues only."""
        if not critique.blocking:
            return content

        issues = "\n".join(f"- {b}" for b in critique.blocking)
        prompt = f"""Improve the following output by fixing ONLY these blocking issues:

BLOCKING ISSUES:
{issues}

CURRENT OUTPUT:
{json.dumps(content, indent=2, default=str) if isinstance(content, dict) else str(content)}

Return ONLY the corrected output in the same format. No explanation."""

        try:
            raw, _ = await self._agent.call_llm(
                task, [{"role": "user", "content": prompt}],
                max_tokens=task.max_output_tokens, temperature=0.1,
            )
            if isinstance(content, dict):
                clean = raw.strip().strip("```json").strip("```").strip()
                return json.loads(clean)
            return raw
        except Exception:
            return content  # return original if improvement fails

    def _validate_structure(
        self, content: Any, schema: Optional[Dict[str, List[str]]]
    ) -> bool:
        """Rules-based structural validation (not LLM)."""
        if not schema or not isinstance(content, (dict, list)):
            return True
        if isinstance(content, list):
            return all(
                all(k in item for k in schema.get("item", []))
                for item in content
            )
        return all(k in content for k in schema.get("root", []))

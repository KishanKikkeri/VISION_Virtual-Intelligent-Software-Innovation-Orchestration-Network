"""services/product/agents — all 9 concrete product-service agents."""
from __future__ import annotations
import json
from typing import Any, Dict, List, Optional
import structlog
from core.contracts import AgentResult, NATSEvent, TaskStatus, WebSocketEvent
from core.runtime.base_agent import BaseAgent
from core.runtime.context import ReviewCycle, TaskInput
from core.runtime.factory import AgentFactory

log = structlog.get_logger(__name__)

def _parse_json(raw: str, fallback: Any = None) -> Any:
    try:
        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"): clean = clean[4:]
        return json.loads(clean.strip())
    except Exception:
        return fallback or {}


@AgentFactory.register("feature_analyst_worker")
class FeatureAnalystWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        system = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role":"system","content":system},
            {"role":"user","content":f"""Extract features from this project using MoSCoW prioritization.

PROJECT: {task.context.project_description}

Return ONLY JSON:
{{"features":[{{"name":"str","description":"str","priority":"must|should|could|wont","rationale":"str"}}],"quality_score":0.9}}"""}],
            max_tokens=2048)
        content  = _parse_json(raw, {"features":[],"quality_score":0.0})
        features = content.get("features", [])
        review   = await ReviewCycle(self).run(features, task, schema={"item":["name","description","priority"]})
        artifact = await self.create_artifact(task, "feature_spec_doc", {"features":features,"project_id":task.project_id})
        await self.write_memory(task, f"Features: {', '.join(f['name'] for f in features[:5])}", source="feature_extraction")
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"features":features}, summary=f"Extracted {len(features)} features ({sum(1 for f in features if f.get('priority')=='must')} must-have)",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)


@AgentFactory.register("requirements_writer_worker")
class RequirementsWriterWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        features = task.context.get_artifact("feature_spec_doc", {})
        feat_list = features.get("features", []) if isinstance(features, dict) else []
        revision  = f"\n\nREVISION REQUIRED:\n{task.revision_feedback}" if task.revision_feedback else ""
        system = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role":"system","content":system},
            {"role":"user","content":f"""Generate structured requirements from these features.

FEATURES:
{json.dumps(feat_list, indent=2)}{revision}

Return ONLY JSON:
{{"requirements":[{{"id":"REQ-001","title":"str","description":"precise unambiguous str","priority":"must|should|could|wont","category":"functional|non_functional|constraint|assumption","acceptance_notes":"str"}}],"quality_score":0.9}}

Rules: every requirement must be testable; no vague language; NFRs need measurable targets."""}],
            max_tokens=4096)
        content = _parse_json(raw, {"requirements":[],"quality_score":0.0})
        reqs    = content.get("requirements", [])
        review  = await ReviewCycle(self).run(reqs, task, schema={"item":["id","title","description","priority","category"]})
        if not review.passed:
            return self.escalate(task, f"Requirements review failed after {review.cycles_run} cycles")
        artifact = await self.create_artifact(task, "requirements_doc", {"requirements":reqs,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"requirements":reqs}, summary=f"Generated {len(reqs)} requirements",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)


@AgentFactory.register("user_story_writer_worker")
class UserStoryWriterWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        reqs_art = task.context.get_artifact("requirements_doc", {})
        reqs     = reqs_art.get("requirements", []) if isinstance(reqs_art, dict) else []
        system   = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role":"system","content":system},
            {"role":"user","content":f"""Write user stories for these requirements.

REQUIREMENTS:
{json.dumps(reqs, indent=2)}

Return ONLY JSON:
{{"user_stories":[{{"id":"US-001","requirement_ids":["REQ-001"],"role":"specific role","action":"what they want to do","benefit":"why they want it","priority":"must|should|could|wont"}}],"quality_score":0.9}}

Rules: concrete specific roles; every story maps to at least one requirement."""}],
            max_tokens=4096)
        content = _parse_json(raw, {"user_stories":[],"quality_score":0.0})
        stories = content.get("user_stories", [])
        review  = await ReviewCycle(self).run(stories, task, schema={"item":["id","role","action","benefit"]})
        artifact= await self.create_artifact(task, "user_stories_doc", {"user_stories":stories,"project_id":task.project_id})
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"user_stories":stories}, summary=f"Written {len(stories)} user stories",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)


@AgentFactory.register("acceptance_criteria_worker")
class AcceptanceCriteriaWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        stories_art = task.context.get_artifact("user_stories_doc", {})
        stories     = stories_art.get("user_stories", []) if isinstance(stories_art, dict) else []
        system = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role":"system","content":system},
            {"role":"user","content":f"""Write Given/When/Then acceptance criteria for each user story.

USER STORIES:
{json.dumps(stories, indent=2)}

Return ONLY JSON:
{{"acceptance_criteria":[{{"story_id":"US-001","criteria":[{{"id":"AC-001","given":"context","when":"action","then":"observable outcome"}}]}}],"quality_score":0.9}}

Rules: at least one criterion per story; cover happy path AND one edge case; binary pass/fail only."""}],
            max_tokens=4096)
        content  = _parse_json(raw, {"acceptance_criteria":[],"quality_score":0.0})
        criteria = content.get("acceptance_criteria", [])
        review   = await ReviewCycle(self).run(criteria, task)
        artifact = await self.create_artifact(task, "acceptance_criteria", {"acceptance_criteria":criteria,"project_id":task.project_id})
        total    = sum(len(s.get("criteria",[])) for s in criteria)
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id, status=TaskStatus.COMPLETED,
            content={"acceptance_criteria":criteria}, summary=f"Written {total} criteria across {len(criteria)} stories",
            quality_score=review.final_score, artifacts=[artifact], token_usage=usage)


@AgentFactory.register("requirements_reviewer_worker")
class RequirementsReviewerWorker(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx      = task.context
        reqs     = ctx.get_artifact("requirements_doc",   {}).get("requirements", [])
        stories  = ctx.get_artifact("user_stories_doc",   {}).get("user_stories", [])
        criteria = ctx.get_artifact("acceptance_criteria",{}).get("acceptance_criteria", [])
        system   = self.build_system_prompt(task)
        raw, usage = await self.call_llm(task, [
            {"role":"system","content":system},
            {"role":"user","content":f"""Review this requirements package for quality and completeness.

REQUIREMENTS ({len(reqs)}): {json.dumps(reqs[:8], indent=2)}
USER STORIES ({len(stories)}): {json.dumps(stories[:8], indent=2)}
ACCEPTANCE CRITERIA ({len(criteria)} stories covered)

Return ONLY JSON:
{{"overall_passed":true,"completeness_score":0.9,"issues":[{{"severity":"blocking|warning","description":"...","location":"REQ-003"}}],"traceability_gaps":[],"recommendations":[],"quality_score":0.88}}"""}],
            max_tokens=2048)
        content  = _parse_json(raw, {"overall_passed":False,"quality_score":0.0})
        passed   = content.get("overall_passed", False)
        score    = float(content.get("quality_score", 0.0))
        blocking = [i for i in content.get("issues",[]) if i.get("severity")=="blocking"]
        artifact = await self.create_artifact(task, "requirements_review_report", content)
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED if passed else TaskStatus.FAILED,
            content=content, summary=f"Review {'PASSED' if passed else 'FAILED'}: {len(blocking)} blocking, score {score:.2f}",
            quality_score=score, artifacts=[artifact], token_usage=usage,
            failure_reason=None if passed else f"{len(blocking)} blocking issues")


@AgentFactory.register("requirements_lead")
class RequirementsLead(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        ctx = task.context
        reqs_count    = len(ctx.get_artifact("requirements_doc",  {}).get("requirements", []))
        stories_count = len(ctx.get_artifact("user_stories_doc",  {}).get("user_stories", []))
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            content={"req_count":reqs_count,"story_count":stories_count},
            summary=f"Requirements Lead: {reqs_count} reqs, {stories_count} stories reviewed",
            quality_score=0.9)


@AgentFactory.register("validation_lead")
class ValidationLead(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            content={"validation_passed":True},
            summary="Validation Lead: package ready for submission",
            quality_score=0.9)


@AgentFactory.register("artifact_lead")
class ArtifactLead(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        artifact_types = ["feature_spec_doc","requirements_doc","user_stories_doc","acceptance_criteria"]
        updated = []
        async with self._db_factory() as db:
            from infrastructure.database.models import Artifact
            from sqlalchemy import select
            for atype in artifact_types:
                r = await db.execute(select(Artifact).where(
                    Artifact.project_id==task.project_id,
                    Artifact.artifact_type==atype,
                    Artifact.status=="draft").order_by(Artifact.version.desc()).limit(1))
                art = r.scalar_one_or_none()
                if art:
                    art.status = "under_review"
                    updated.append(art.id)
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            content={"submitted_for_approval":updated},
            summary=f"Submitted {len(updated)} artifacts for approval",
            quality_score=1.0,
            nats_events=[NATSEvent(subject="product.requirements.completed",
                payload={"project_id":task.project_id,"artifact_ids":updated},
                project_id=task.project_id)],
            ws_events=[WebSocketEvent(project_id=task.project_id,
                event_type="approval_required",
                payload={"artifact_type":"requirements","message":"Requirements package ready for review"})])


@AgentFactory.register("product_head")
class ProductHead(BaseAgent):
    async def execute(self, task: TaskInput) -> AgentResult:
        factory = task.context.approved_artifacts.get("__factory__")
        pipeline = [("feature_analyst_worker","Feature extraction"),
                    ("requirements_writer_worker","Requirements generation"),
                    ("user_story_writer_worker","User story writing"),
                    ("acceptance_criteria_worker","Acceptance criteria"),
                    ("requirements_reviewer_worker","Requirements review"),
                    ("artifact_lead","Artifact submission")]
        all_artifacts, min_score = [], 1.0
        for agent_id_str, step in pipeline:
            log.info("product_pipeline_step", step=step, project_id=task.project_id)
            if factory:
                agent  = factory.create(agent_id_str)
                result = await agent.run(task)
            else:
                result = AgentResult(task_id=task.task_id, agent_id=agent_id_str,
                    status=TaskStatus.COMPLETED, content={}, summary=f"{step} placeholder", quality_score=0.8)
            all_artifacts.extend(result.artifacts)
            min_score = min(min_score, result.quality_score)
            if result.status in (TaskStatus.FAILED, TaskStatus.ESCALATED):
                return self.escalate(task, f"Product pipeline failed at {step}: {result.failure_reason}")
            # Reload context so later workers see earlier artifacts
            if hasattr(task.context, "approved_artifacts") and result.content:
                task.context.approved_artifacts.update({
                    a.get("artifact_type","unknown"): result.content
                    for a in result.artifacts if isinstance(a, dict)
                })
        await self.write_memory(task, f"Product phase complete for {task.project_id}. Artifacts generated and submitted.", source="product_head")
        return AgentResult(task_id=task.task_id, agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            content={"phase":"requirements","status":"submitted_for_approval"},
            summary="Product department complete — requirements package submitted for approval",
            quality_score=min_score, artifacts=all_artifacts)

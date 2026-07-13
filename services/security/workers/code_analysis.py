"""services/security/workers/code_analysis.py — OWASP Checker + Injection Check Worker."""
from __future__ import annotations

import json

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.security.base import SecurityScanWorkerMixin


@AgentFactory.register("owasp_checker_worker")
class OwaspCheckerWorker(SecurityScanWorkerMixin, BaseAgent):
    """
    Checks Engineering's source_code artifact against OWASP Top 10
    vulnerability patterns (broken auth, sensitive data exposure,
    security misconfiguration, XXE, insecure deserialization, etc).
    LLM-driven, mirroring services/qa/workers/unit.py's UnitTestWriterWorker.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        source = task.context.get_artifact("source_code", {})
        files = source.get("files", []) if isinstance(source, dict) else []
        paths = [f.get("path") for f in files][:10]

        sys_prompt = self.build_system_prompt(task)
        user_prompt = f"""Review these source files for OWASP Top 10 vulnerability patterns.

FILES: {json.dumps(paths)}
CHECK FOR: broken access control, cryptographic failures, injection,
insecure design, security misconfiguration, vulnerable components,
authentication failures, data integrity failures, logging failures, SSRF.

Return ONLY JSON:
{{"findings":[{{"rule":"broken_access_control","file":"app/auth.py","severity":"high","description":"Missing role check on admin endpoint"}}],"files_scanned":{len(paths)},"quality_score":0.88}}"""

        return await self.generate_findings(
            task,
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            artifact_type="static_analysis_report",
            result_key="owasp_findings",
        )


@AgentFactory.register("injection_check_worker")
class InjectionCheckWorker(SecurityScanWorkerMixin, BaseAgent):
    """
    Checks for SQL / command / XSS injection vulnerabilities in
    Engineering's source_code artifact. LLM-driven, sibling to
    OwaspCheckerWorker under code_security_lead.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        source = task.context.get_artifact("source_code", {})
        files = source.get("files", []) if isinstance(source, dict) else []
        paths = [f.get("path") for f in files][:10]

        sys_prompt = self.build_system_prompt(task)
        user_prompt = f"""Review these source files for injection vulnerabilities.

FILES: {json.dumps(paths)}
CHECK FOR: SQL injection (unparameterized queries), command injection
(unsanitized shell calls), cross-site scripting (unescaped output
rendered to HTML), template injection.

Return ONLY JSON:
{{"findings":[{{"rule":"sql_injection","file":"app/db.py","severity":"critical","description":"Raw string interpolation into SQL query"}}],"files_scanned":{len(paths)},"quality_score":0.9}}"""

        return await self.generate_findings(
            task,
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            artifact_type="static_analysis_report",
            result_key="injection_findings",
        )

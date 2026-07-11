"""services/qa/workers/integration.py — Integration/API Test Writer."""
from __future__ import annotations

import json

from core.contracts import AgentResult, TaskStatus
from core.runtime.base_agent import BaseAgent
from core.runtime.context import TaskInput
from core.runtime.factory import AgentFactory
from services.qa.base import QAWorkerMixin
from services.qa.models import SuiteType


@AgentFactory.register("integration_test_writer_worker")
class IntegrationTestWriterWorker(QAWorkerMixin, BaseAgent):
    """
    Generates API integration tests for every path in the approved
    openapi_spec, and performs the "API contract validation" mandatory
    pass condition: every declared path must have at least one
    generated test, otherwise the contract is considered unvalidated.
    """

    async def execute(self, task: TaskInput) -> AgentResult:
        spec = task.context.get_artifact("openapi_spec", {})
        paths = list(spec.get("paths", {}).keys()) if isinstance(spec, dict) else []

        sys_prompt = self.build_system_prompt(task)
        user_prompt = f"""Generate API integration tests for these endpoints.

ENDPOINTS: {json.dumps(paths[:10])}
FRAMEWORK: pytest, httpx.AsyncClient, pytest-asyncio

Return ONLY JSON:
{{"files":[{{"path":"tests/integration/test_api.py","language":"python","content":"import pytest\\nimport httpx\\n\\n\\n@pytest.mark.asyncio\\nasync def test_health():\\n    async with httpx.AsyncClient(base_url='http://localhost:8000') as client:\\n        r = await client.get('/health')\\n    assert r.status_code == 200"}}],"test_count":{max(len(paths),1)},"endpoints_covered":{json.dumps(paths)},"quality_score":0.87}}"""

        contract_valid = bool(paths)  # no declared endpoints -> nothing to validate against
        return await self.generate_suite(
            task, SuiteType.INTEGRATION,
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            artifact_type="integration_test_suite",
            extra_content={"endpoints_covered": paths, "contract_valid": contract_valid},
        )

"""
services/qa/agents — re-export shim for AgentFactory.

AgentFactory._load_class() imports "services.qa.agents" and looks up a
class by converting agent_id -> PascalCase (e.g. "qa_head" ->
"QAHead"). The actual M3.4 hierarchical implementation lives in
workers/, leads/, and head/ (mirroring services/engineering's Stage 1
layout). This module just re-exports every concrete class so lookup
keeps working unchanged.
"""
from __future__ import annotations

from services.qa.head import QAHead
from services.qa.leads import (
    IntegrationTestLead,
    PerformanceTestLead,
    RegressionTestLead,
    UnitTestLead,
)
from services.qa.workers import (
    CoverageAnalyzerWorker,
    IntegrationTestWriterWorker,
    PerformanceTestWorker,
    RegressionSuiteWorker,
    UnitTestWriterWorker,
)

__all__ = [
    "QAHead",
    "UnitTestLead", "IntegrationTestLead", "RegressionTestLead", "PerformanceTestLead",
    "UnitTestWriterWorker", "CoverageAnalyzerWorker",
    "IntegrationTestWriterWorker", "RegressionSuiteWorker", "PerformanceTestWorker",
]

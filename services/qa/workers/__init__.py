"""services/qa/workers — 5 QA worker agents (L5), registered on import."""
from __future__ import annotations

from services.qa.workers.unit import CoverageAnalyzerWorker, UnitTestWriterWorker
from services.qa.workers.integration import IntegrationTestWriterWorker
from services.qa.workers.regression import RegressionSuiteWorker
from services.qa.workers.performance import PerformanceTestWorker

__all__ = [
    "UnitTestWriterWorker",
    "CoverageAnalyzerWorker",
    "IntegrationTestWriterWorker",
    "RegressionSuiteWorker",
    "PerformanceTestWorker",
]

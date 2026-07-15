"""
services/incident_response/providers/base.py — provider interfaces.
=======================================================================
Per the handover's §8 Provider Pattern: every external dependency goes
through a thin provider. No worker calls infrastructure directly.

Two shapes exist here (mirrors services/monitoring/providers/base.py's
MetricsProvider precedent, extended for Incident Response's needs):

  EvidenceProvider — read-only correlation lookups (Monitoring/DevOps/
                     Repository tables). MUST NOT raise; degrade to an
                     empty evidence list rather than aborting analysis.

  ActionProvider   — side-effecting calls out of the Incident Response
                     Service (triggering a DevOps rollback, sending a
                     notification). MUST NOT raise; return a result
                     object describing success/failure instead, so
                     workers can record the outcome rather than crash.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from services.incident_response.models import EvidenceItem


class EvidenceProvider(ABC):
    """Every concrete evidence provider must implement collect()."""

    source: str

    @abstractmethod
    async def collect(self, component: str) -> List[EvidenceItem]:
        """
        Collects zero or more EvidenceItem rows correlated to `component`.
        Must never raise — on internal failure, return an empty list
        (a provider that can't reach its source contributes no evidence
        rather than aborting the whole analysis step).
        """
        raise NotImplementedError

    def _empty(self) -> List[EvidenceItem]:
        return []

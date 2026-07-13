"""
services/security/agents — re-export shim for AgentFactory.

AgentFactory._load_class() imports "services.security.agents" and looks
up a class by converting agent_id -> PascalCase (e.g. "security_head" ->
"SecurityHead"). The actual M3.5 hierarchical implementation lives in
workers/, leads/, and head/ (mirroring services/qa's Stage 1 layout).
This module just re-exports every concrete class so lookup keeps
working unchanged.
"""
from __future__ import annotations

from services.security.head import SecurityHead
from services.security.leads import (
    CodeSecurityLead,
    ComplianceLead,
    DependencyScanLead,
)
from services.security.workers import (
    ComplianceValidatorWorker,
    CveScannerWorker,
    InjectionCheckWorker,
    OwaspCheckerWorker,
    SecretScannerWorker,
)

__all__ = [
    "SecurityHead",
    "DependencyScanLead", "CodeSecurityLead", "ComplianceLead",
    "CveScannerWorker", "OwaspCheckerWorker", "SecretScannerWorker",
    "InjectionCheckWorker", "ComplianceValidatorWorker",
]

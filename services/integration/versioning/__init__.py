"""
services/integration/versioning
================================
Workflow versioning and compatibility package.
"""
from services.integration.versioning.compatibility_checker import (
    CompatibilityResult,
    check_compatibility,
)
from services.integration.versioning.version_registry import (
    VersionDiff,
    VersionRegistry,
    WorkflowVersionRecord,
    default_registry,
    register_current_version,
)

__all__ = [
    "CompatibilityResult",
    "check_compatibility",
    "VersionDiff",
    "VersionRegistry",
    "WorkflowVersionRecord",
    "default_registry",
    "register_current_version",
]

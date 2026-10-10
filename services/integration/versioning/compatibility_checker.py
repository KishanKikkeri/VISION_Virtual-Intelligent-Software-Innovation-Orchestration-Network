"""
services/integration/versioning/compatibility_checker.py
========================================================
M4.1 Workflow Versioning — compatibility checks between workflow versions.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from services.integration.versioning.version_registry import (
    VersionRegistry,
    default_registry,
)


class CompatibilityResult(BaseModel):
    workflow: str
    from_version: str
    to_version: str
    compatible: bool
    checkpoint_node: Optional[str] = None
    reasons: List[str] = Field(default_factory=list)


def check_compatibility(
    workflow: str,
    from_version: str,
    to_version: str,
    checkpoint_node: Optional[str] = None,
    registry: Optional[VersionRegistry] = None,
) -> CompatibilityResult:
    reg = registry or default_registry
    rec_from = reg.get(workflow, from_version)
    rec_to = reg.get(workflow, to_version)

    reasons: List[str] = []
    if rec_from is None or rec_to is None:
        missing = []
        if rec_from is None:
            missing.append(from_version)
        if rec_to is None:
            missing.append(to_version)
        return CompatibilityResult(
            workflow=workflow,
            from_version=from_version,
            to_version=to_version,
            compatible=False,
            checkpoint_node=checkpoint_node,
            reasons=[f"versions not found: {missing}"],
        )

    if from_version == to_version:
        return CompatibilityResult(
            workflow=workflow,
            from_version=from_version,
            to_version=to_version,
            compatible=True,
            checkpoint_node=checkpoint_node,
            reasons=["identical versions"],
        )

    compatible = True
    if checkpoint_node is not None:
        if checkpoint_node not in rec_to.nodes:
            compatible = False
            reasons.append(
                f"checkpoint node {repr(checkpoint_node)} absent in target version {repr(to_version)}"
            )
        else:
            reasons.append(
                f"checkpoint node {repr(checkpoint_node)} is preserved in {repr(to_version)}"
            )

    diff = reg.diff_versions(workflow, from_version, to_version)
    if diff and diff.is_breaking:
        if checkpoint_node is None:
            compatible = False
        reasons.append(
            f"breaking change between {from_version} and {to_version}: removed nodes {diff.removed_nodes}"
        )

    return CompatibilityResult(
        workflow=workflow,
        from_version=from_version,
        to_version=to_version,
        compatible=compatible,
        checkpoint_node=checkpoint_node,
        reasons=reasons,
    )

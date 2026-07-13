"""
services/devops/providers/docker_compose.py — local/simulated deployment provider.
======================================================================================
This sandbox has no live Docker daemon, cloud account, or Kubernetes
cluster to deploy to (same constraint the M3.5 CVE/license lookup
tables were built around). DockerComposeProvider is a deterministic
stand-in: it "deploys" by validating the compose spec's shape and
succeeds unless explicitly told to fail via an override hook — the
same override-hook pattern Security's cve_scanner_worker and
compliance_validator_worker already use for deterministic test control
(`__dependency_manifest_override__`).

Swap this for a real `docker compose up -d` / SSH / cloud-API-backed
provider in a live deployment environment — ProvisionerWorker only
depends on the DeploymentProvider interface (providers/base.py), not on
this implementation.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from services.devops.providers.base import DeploymentProvider


class DockerComposeProvider(DeploymentProvider):
    name = "docker_compose"

    def __init__(self, force_deploy_failure: bool = False,
                 health_override: Optional[Dict[str, bool]] = None,
                 force_rollback_failure: bool = False):
        # Hooks purely for deterministic testing — a live provider
        # wouldn't take these.
        self._force_deploy_failure = force_deploy_failure
        self._health_override = health_override
        self._force_rollback_failure = force_rollback_failure

    async def deploy(self, project_id: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        if self._force_deploy_failure or not plan.get("compose_ref"):
            return {"success": False, "deployment_ref": "", "detail": "compose spec missing or deploy forced to fail"}
        return {"success": True, "deployment_ref": f"local-compose::{project_id}", "detail": "stack started"}

    async def health_check(self, project_id: str, deployment_ref: str) -> Dict[str, Any]:
        from services.devops.models import REQUIRED_HEALTH_CHECKS

        overrides = self._health_override or {}
        checks = [
            {"check_name": name, "passed": bool(overrides.get(name, True)),
             "detail": "ok" if overrides.get(name, True) else "check failed"}
            for name in REQUIRED_HEALTH_CHECKS
        ]
        return {"checks": checks}

    async def rollback(self, project_id: str, deployment_ref: str, reason: str) -> Dict[str, Any]:
        if self._force_rollback_failure:
            return {"success": False, "detail": "rollback failed — manual intervention required"}
        return {"success": True, "detail": f"rolled back {deployment_ref}: {reason}"}

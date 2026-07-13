"""
services/devops/providers/base.py — Deployment provider interface.
=======================================================================
A DeploymentProvider is the abstraction between DevOps's Provisioner
Worker and whatever actually executes a deployment (a local Docker
Compose stack today; a cloud/K8s orchestrator eventually). Swapping the
implementation should never require changing ProvisionerWorker.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class DeploymentProvider(ABC):
    """Every concrete provider must implement deploy/health_check/rollback."""

    name: str = "base"

    @abstractmethod
    async def deploy(self, project_id: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Executes a deployment from an approved DeploymentPlan dict. Returns a result dict
        with at least {"success": bool, "deployment_ref": str}."""
        raise NotImplementedError

    @abstractmethod
    async def health_check(self, project_id: str, deployment_ref: str) -> Dict[str, Any]:
        """Runs post-deploy health checks. Returns {"checks": [{"check_name","passed","detail"}]}."""
        raise NotImplementedError

    @abstractmethod
    async def rollback(self, project_id: str, deployment_ref: str, reason: str) -> Dict[str, Any]:
        """Rolls a failed deployment back. Returns {"success": bool, "detail": str}."""
        raise NotImplementedError

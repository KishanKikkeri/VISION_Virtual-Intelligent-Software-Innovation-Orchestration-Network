"""
services/devops/providers/kubernetes.py — V2 placeholder ONLY.

Per the M3.6 spec: "Kubernetes Worker — V2 placeholder only. Do NOT
implement Kubernetes deployment. Only generate interfaces." This class
exists so the DeploymentProvider interface has a documented future
implementer, but every method deliberately raises NotImplementedError.
No agent_id is reserved in AGENT_REGISTRY for a "kubernetes_worker" —
there is intentionally no worker wired to this provider in M3.6.
"""
from __future__ import annotations

from typing import Any, Dict

from services.devops.providers.base import DeploymentProvider


class KubernetesProvider(DeploymentProvider):
    name = "kubernetes"

    async def deploy(self, project_id: str, plan: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError("Kubernetes deployment is a V2 placeholder — not implemented in M3.6")

    async def health_check(self, project_id: str, deployment_ref: str) -> Dict[str, Any]:
        raise NotImplementedError("Kubernetes health checks are a V2 placeholder — not implemented in M3.6")

    async def rollback(self, project_id: str, deployment_ref: str, reason: str) -> Dict[str, Any]:
        raise NotImplementedError("Kubernetes rollback is a V2 placeholder — not implemented in M3.6")

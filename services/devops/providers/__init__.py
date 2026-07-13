"""services/devops/providers — deployment provider abstraction + implementations."""
from __future__ import annotations

from services.devops.providers.base import DeploymentProvider
from services.devops.providers.docker_compose import DockerComposeProvider
from services.devops.providers.kubernetes import KubernetesProvider

__all__ = ["DeploymentProvider", "DockerComposeProvider", "KubernetesProvider"]


def default_provider() -> DeploymentProvider:
    """The provider used when nothing else is configured."""
    return DockerComposeProvider()

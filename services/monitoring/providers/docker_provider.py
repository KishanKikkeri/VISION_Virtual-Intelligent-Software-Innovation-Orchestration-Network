"""services/monitoring/providers/docker_provider.py — read-only Docker Engine health.

Per spec §6: read-only `stats`/`ps` only — never `docker exec`,
`build`, or `run`. Uses the Docker Engine API via the `docker` SDK
(added to pyproject.toml for M3.7, see Appendix).
"""
from __future__ import annotations

from typing import List

from services.monitoring.models import MonitoredComponent
from services.monitoring.providers.base import MetricsProvider


class DockerProvider(MetricsProvider):
    component = MonitoredComponent.DOCKER

    def __init__(self, client=None):
        # Lazily created if not supplied — keeps this importable in
        # environments/tests without a Docker daemon reachable.
        self._client = client

    def _get_client(self):
        if self._client is not None:
            return self._client
        import docker  # local import — optional runtime dependency
        return docker.from_env()

    async def collect(self) -> List:
        try:
            client = self._get_client()
            containers = client.containers.list(all=True)
            if not containers:
                return self._healthy("docker_containers_running", 100.0, container_count="0")
            running = sum(1 for c in containers if getattr(c, "status", "") == "running")
            ratio = running / len(containers)
            return self._healthy(
                "docker_containers_running", round(ratio * 100.0, 2),
                container_count=str(len(containers)), running=str(running),
            )
        except Exception as e:
            return self._degraded(str(e))

"""
services/integration/production/deployment_validator.py
=================================
M4.9 §4 Deployment Validator — "no deployment should start with invalid
configuration." Validates the *shape* of Docker Compose files,
Kubernetes manifests, Helm values, and environment configuration
documents — structural/schema-level checks (required keys present,
correct nesting), not a live cluster dry-run (this package has no
Docker/Kubernetes client dependency at all; see module docstring on
`environment_validator.py` for the same "no live client" boundary).

YAML parsing is optional exactly like `configuration_manager.load`: if
PyYAML isn't installed, each `validate_*` function returns a single
`DeploymentValidationIssue` explaining that rather than raising.
"""
from __future__ import annotations

from typing import Any, Dict, List

from services.integration.production.configuration_manager import validate as validate_config
from services.integration.production.release_models import (
    DeploymentAssetKind, DeploymentValidationIssue, DeploymentValidationResult, Environment,
)


def _try_yaml_load_all(text: str) -> List[Dict[str, Any]]:
    import yaml
    return [doc for doc in yaml.safe_load_all(text) if doc]


def validate_docker_compose(text: str, name: str = "docker-compose.yml") -> List[DeploymentValidationIssue]:
    issues: List[DeploymentValidationIssue] = []
    try:
        docs = _try_yaml_load_all(text)
    except ImportError:
        return [DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.DOCKER_COMPOSE,
                                            message="PyYAML is not installed; cannot parse compose file")]
    except Exception as e:  # noqa: BLE001
        return [DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.DOCKER_COMPOSE,
                                            message=f"invalid YAML: {e}")]
    if not docs:
        return [DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.DOCKER_COMPOSE,
                                            message="compose file is empty")]
    doc = docs[0]
    services = doc.get("services")
    if not services or not isinstance(services, dict):
        issues.append(DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.DOCKER_COMPOSE,
                                                   message="no 'services' section found"))
        return issues
    for svc_name, svc in services.items():
        if not isinstance(svc, dict) or ("image" not in svc and "build" not in svc):
            issues.append(DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.DOCKER_COMPOSE,
                                                       message=f"service {svc_name!r} has neither 'image' nor 'build'"))
    return issues


def validate_kubernetes_manifest(text: str, name: str = "manifest.yaml") -> List[DeploymentValidationIssue]:
    issues: List[DeploymentValidationIssue] = []
    try:
        docs = _try_yaml_load_all(text)
    except ImportError:
        return [DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.KUBERNETES_MANIFEST,
                                            message="PyYAML is not installed; cannot parse manifest")]
    except Exception as e:  # noqa: BLE001
        return [DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.KUBERNETES_MANIFEST,
                                            message=f"invalid YAML: {e}")]
    if not docs:
        return [DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.KUBERNETES_MANIFEST,
                                            message="manifest is empty")]
    for i, doc in enumerate(docs):
        label = f"{name}[{i}]"
        for required in ("apiVersion", "kind"):
            if required not in doc:
                issues.append(DeploymentValidationIssue(asset=label, kind=DeploymentAssetKind.KUBERNETES_MANIFEST,
                                                           message=f"missing required field {required!r}"))
        metadata = doc.get("metadata")
        if not isinstance(metadata, dict) or not metadata.get("name"):
            issues.append(DeploymentValidationIssue(asset=label, kind=DeploymentAssetKind.KUBERNETES_MANIFEST,
                                                       message="missing metadata.name"))
    return issues


def validate_helm_values(text: str, name: str = "values.yaml") -> List[DeploymentValidationIssue]:
    issues: List[DeploymentValidationIssue] = []
    try:
        docs = _try_yaml_load_all(text)
    except ImportError:
        return [DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.HELM_VALUES,
                                            message="PyYAML is not installed; cannot parse values file")]
    except Exception as e:  # noqa: BLE001
        return [DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.HELM_VALUES,
                                            message=f"invalid YAML: {e}")]
    if not docs or not isinstance(docs[0], dict):
        issues.append(DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.HELM_VALUES,
                                                   message="values file is empty or not a mapping"))
        return issues
    doc = docs[0]
    for required in ("image", "replicaCount"):
        if required not in doc:
            issues.append(DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.HELM_VALUES,
                                                       message=f"missing recommended key {required!r}",
                                                       severity="warning"))
    return issues


def validate_environment_config(config: Dict[str, Any], environment: Environment | str,
                                 name: str = "environment_config") -> List[DeploymentValidationIssue]:
    result = validate_config(config, environment)
    return [DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.ENVIRONMENT_CONFIG,
                                        message=i.message, severity=i.severity) for i in result.errors + result.warnings]


def validate_deployment(assets: Dict[str, str], environment: Environment | str = Environment.PRODUCTION,
                         environment_config: Dict[str, Any] | None = None) -> DeploymentValidationResult:
    """Aggregates every asset kind named in §12 (`assets` maps a
    filename to its raw text content). Filenames are dispatched by
    extension/name heuristic: `docker-compose*` -> Compose,
    `values.yaml`/`values.yml` -> Helm, everything else ending in
    `.yaml`/`.yml` -> a Kubernetes manifest."""
    all_issues: List[DeploymentValidationIssue] = []
    checked: List[str] = []

    for name, text in assets.items():
        checked.append(name)
        lower = name.lower()
        if "docker-compose" in lower or "compose" in lower:
            all_issues.extend(validate_docker_compose(text, name))
        elif lower.endswith(("values.yaml", "values.yml")):
            all_issues.extend(validate_helm_values(text, name))
        elif lower.endswith((".yaml", ".yml")):
            all_issues.extend(validate_kubernetes_manifest(text, name))
        else:
            all_issues.append(DeploymentValidationIssue(asset=name, kind=DeploymentAssetKind.ENVIRONMENT_CONFIG,
                                                          message="unrecognized asset type; skipped",
                                                          severity="warning"))

    if environment_config is not None:
        checked.append("environment_config")
        all_issues.extend(validate_environment_config(environment_config, environment))

    valid = not any(i.severity == "error" for i in all_issues)
    return DeploymentValidationResult(valid=valid, assets_checked=checked, issues=all_issues)

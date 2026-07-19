"""
core/llm/router.py
===================
LLM provider + model selection logic.
The first piece of W12 (Task Delegation Graph).

Input:  task_type, agent_role, project's preferred provider, budget state
Output: (provider_name, model_name)

Failover:
  If primary provider fails, the router automatically tries the next
  available provider in the failover chain.
  Agents never know which provider actually served the request.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional, Tuple

import structlog

from core.config.settings import get_settings

log = structlog.get_logger(__name__)


class ModelTier(str, Enum):
    ECONOMY  = "economy"    # high-volume, low-complexity (changelogs, readme)
    STANDARD = "standard"   # default worker tasks
    ADVANCED = "advanced"   # lead/head reviews, complex reasoning
    PREMIUM  = "premium"    # security, architecture, RCA, escalated tasks


# Tier assignment by agent role
ROLE_TO_TIER: Dict[str, ModelTier] = {
    "worker":  ModelTier.STANDARD,
    "lead":    ModelTier.ADVANCED,
    "head":    ModelTier.ADVANCED,
    "manager": ModelTier.STANDARD,
}

# Task-type overrides (take priority over role-based tier)
TASK_TIER_OVERRIDES: Dict[str, ModelTier] = {
    # Premium — complex reasoning or high-stakes outputs
    "security_scan":           ModelTier.PREMIUM,
    "owasp_check":             ModelTier.PREMIUM,
    "architecture_review":     ModelTier.PREMIUM,
    "root_cause_analysis":     ModelTier.PREMIUM,
    "traceability_check":      ModelTier.ADVANCED,
    "api_design":              ModelTier.ADVANCED,
    "database_design":         ModelTier.ADVANCED,
    # Economy — simple generation, low stakes
    "generate_changelog":      ModelTier.ECONOMY,
    "generate_readme":         ModelTier.ECONOMY,
    "generate_code_comments":  ModelTier.ECONOMY,
    "generate_env_template":   ModelTier.ECONOMY,
}

# Model by provider × tier
PROVIDER_TIER_MODELS: Dict[str, Dict[ModelTier, str]] = {
    "anthropic": {
        ModelTier.ECONOMY:  "claude-haiku-4-5",
        ModelTier.STANDARD: "claude-sonnet-4-6",
        ModelTier.ADVANCED: "claude-sonnet-4-6",
        ModelTier.PREMIUM:  "claude-opus-4-6",
    },
    "openai": {
        ModelTier.ECONOMY:  "gpt-4o-mini",
        ModelTier.STANDARD: "gpt-4o",
        ModelTier.ADVANCED: "gpt-4o",
        ModelTier.PREMIUM:  "o3",
    },
    "gemini": {
        ModelTier.ECONOMY:  "gemini-2.0-flash",
        ModelTier.STANDARD: "gemini-2.0-flash",
        ModelTier.ADVANCED: "gemini-2.5-pro",
        ModelTier.PREMIUM:  "gemini-2.5-pro",
    },
    "ollama": {
        ModelTier.ECONOMY:  "mistral",
        ModelTier.STANDARD: "llama3.3",
        ModelTier.ADVANCED: "llama3.3",
        ModelTier.PREMIUM:  "deepseek-r1",
    },
    "openrouter": {
        ModelTier.ECONOMY:  "meta-llama/llama-3.3-70b-instruct",
        ModelTier.STANDARD: "anthropic/claude-sonnet-4-6",
        ModelTier.ADVANCED: "anthropic/claude-sonnet-4-6",
        ModelTier.PREMIUM:  "openrouter/auto",
    },
}

# Failover chain: if provider[0] fails, try provider[1], etc.
FAILOVER_CHAINS: Dict[str, List[str]] = {
    "anthropic":  ["anthropic", "openai",     "openrouter", "ollama"],
    "openai":     ["openai",    "anthropic",   "openrouter", "ollama"],
    "gemini":     ["gemini",    "anthropic",   "openai",     "ollama"],
    "ollama":     ["ollama",    "openrouter",  "anthropic",  "openai"],
    "openrouter": ["openrouter","anthropic",   "openai",     "ollama"],
}


def select_provider_and_model(
    preferred_provider: str,
    agent_role:         str,
    task_type:          str,
    escalation_level:   int  = 0,
    budget_tight:       bool = False,
    available_providers:Optional[List[str]] = None,
) -> Tuple[str, str]:
    """
    Returns (provider_name, model_name) for a given task.

    Args:
        preferred_provider:  Project's configured LLM provider.
        agent_role:          "worker" | "lead" | "head" | "manager"
        task_type:           e.g. "generate_requirements", "security_scan"
        escalation_level:    0 = normal, 1+ = bump model tier up by N levels
        budget_tight:        True = downgrade by one tier if not escalating
        available_providers: Override the available provider list (for tests)
    """
    tier_order = [
        ModelTier.ECONOMY, ModelTier.STANDARD,
        ModelTier.ADVANCED, ModelTier.PREMIUM,
    ]

    # Determine base tier
    base_tier = TASK_TIER_OVERRIDES.get(
        task_type,
        ROLE_TO_TIER.get(agent_role, ModelTier.STANDARD),
    )
    tier_idx = tier_order.index(base_tier)

    # Apply escalation (upgrades tier)
    tier_idx = min(tier_idx + escalation_level, len(tier_order) - 1)

    # Apply budget downgrade (only when not escalating)
    if budget_tight and escalation_level == 0:
        tier_idx = max(tier_idx - 1, 0)

    selected_tier = tier_order[tier_idx]

    # Walk the failover chain to find an available provider
    chain = FAILOVER_CHAINS.get(preferred_provider, [preferred_provider])
    avail = set(available_providers or _get_available_providers())

    for provider in chain:
        if provider in avail:
            models = PROVIDER_TIER_MODELS.get(provider, {})
            model  = models.get(selected_tier, models.get(ModelTier.STANDARD, ""))
            if model:
                log.debug("model_selected",
                          provider=provider, model=model,
                          tier=selected_tier, task=task_type)
                return provider, model

    # Ultimate fallback — should not reach here if NATS + secrets are validated
    raise RuntimeError(
        f"No available LLM provider found. "
        f"Preferred: {preferred_provider}. Available: {avail}"
    )


def _get_available_providers() -> List[str]:
    """Reads which providers have API keys configured."""
    settings   = get_settings()
    available  = []
    if settings.anthropic_api_key:   available.append("anthropic")
    if settings.openai_api_key:      available.append("openai")
    if settings.gemini_api_key:      available.append("gemini")
    if settings.openrouter_api_key:  available.append("openrouter")
    available.append("ollama")   # always try ollama (local, no key)
    return available

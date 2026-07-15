"""
infrastructure/secrets/validator.py
=====================================
Sprint 3 — Secrets Module.
Validates required secrets at startup. Application refuses to start
if any required secret is missing or obviously invalid.
Logs a clear, actionable message for each missing secret.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class SecretSpec:
    name:        str
    required:    bool
    description: str
    min_length:  Optional[int] = None
    validator:   Optional[callable] = None


REQUIRED_SECRETS: List[SecretSpec] = [
    SecretSpec(
        name="DATABASE_URL",
        required=True,
        description="PostgreSQL connection string (postgresql+asyncpg://...)",
        validator=lambda v: v.startswith("postgresql"),
    ),
    SecretSpec(
        name="JWT_SECRET",
        required=True,
        description="JWT signing secret. Generate: openssl rand -hex 32",
        min_length=32,
    ),
    SecretSpec(
        name="NATS_URL",
        required=True,
        description="NATS server URL (nats://host:4222)",
        validator=lambda v: v.startswith("nats://"),
    ),
]

OPTIONAL_SECRETS: List[SecretSpec] = [
    SecretSpec("ANTHROPIC_API_KEY",  required=False, description="Anthropic Claude API key"),
    SecretSpec("OPENAI_API_KEY",     required=False, description="OpenAI API key"),
    SecretSpec("GEMINI_API_KEY",     required=False, description="Google Gemini API key"),
    SecretSpec("OPENROUTER_API_KEY", required=False, description="OpenRouter API key"),
]


def validate_secrets(exit_on_failure: bool = True) -> bool:
    """
    Validates all required secrets are present and valid.
    Logs warnings for missing optional secrets.

    Args:
        exit_on_failure: if True (default), calls sys.exit(1) on failure.
                         Set False in tests.

    Returns:
        True if all required secrets are valid.
    """
    errors: List[str] = []
    warnings: List[str] = []

    # Check required secrets
    for spec in REQUIRED_SECRETS:
        value = os.getenv(spec.name, "").strip()

        if not value:
            errors.append(
                f"  ✗ {spec.name} is required but not set.\n"
                f"    → {spec.description}"
            )
            continue

        if spec.min_length and len(value) < spec.min_length:
            errors.append(
                f"  ✗ {spec.name} is too short "
                f"({len(value)} chars, minimum {spec.min_length}).\n"
                f"    → {spec.description}"
            )
            continue

        if spec.validator and not spec.validator(value):
            errors.append(
                f"  ✗ {spec.name} failed validation.\n"
                f"    → {spec.description}"
            )
            continue

        log.debug("secret_ok", secret=spec.name)

    # Check optional secrets (warn if ALL are missing — at least one LLM required)
    available_llm_keys = [
        s.name for s in OPTIONAL_SECRETS
        if os.getenv(s.name, "").strip()
    ]

    if not available_llm_keys:
        warnings.append(
            "  ⚠ No LLM provider API keys found. "
            "Set at least one of: ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "GEMINI_API_KEY, OPENROUTER_API_KEY.\n"
            "    → Ollama (local) will be used if available at OLLAMA_BASE_URL."
        )
    else:
        log.info("llm_providers_available", providers=available_llm_keys)

    # Log warnings
    for w in warnings:
        log.warning("secret_warning", message=w)

    # Handle errors
    if errors:
        error_block = "\n".join(errors)
        log.critical(
            "startup_failed_missing_secrets",
            message=(
                "\n\n╔══════════════════════════════════════════════════╗\n"
                "║  AASC STARTUP FAILED — Missing required secrets  ║\n"
                "╚══════════════════════════════════════════════════╝\n\n"
                f"{error_block}\n\n"
                "  Copy .env.example to .env and fill in the required values.\n"
                "  See docs/setup.md for detailed instructions.\n"
            ),
        )
        if exit_on_failure:
            sys.exit(1)
        return False

    log.info("secrets_validated", required_count=len(REQUIRED_SECRETS))
    return True


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """
    Retrieves a secret by name. In V2 this will be replaced with
    HashiCorp Vault or AWS Secrets Manager. All secret reads
    must go through this function — never os.getenv() directly
    in infrastructure or agent code.
    """
    return os.getenv(name, default)

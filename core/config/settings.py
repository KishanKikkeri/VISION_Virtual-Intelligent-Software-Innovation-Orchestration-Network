"""
core/config/settings.py
========================
Single source of truth for all configuration.
Loaded once at startup via get_settings() (cached).
All infrastructure modules import from here — never os.getenv() directly.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────
    app_env:      str = "development"
    app_host:     str = "0.0.0.0"
    app_port:     int = 8000
    app_workers:  int = 1
    cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",")]

    # ── Database ─────────────────────────────────────────────
    database_url: str   # Required — startup fails without this

    @property
    def database_url_sync(self) -> str:
        """Synchronous URL for Alembic migrations."""
        return self.database_url.replace("+asyncpg", "")

    # ── Messaging ────────────────────────────────────────────
    nats_url: str = "nats://localhost:4222"  # Required

    # ── Vector memory ────────────────────────────────────────
    qdrant_url:     str = "http://localhost:6333"
    qdrant_api_key: Optional[str] = None

    # ── Auth ─────────────────────────────────────────────────
    jwt_secret:            str  # Required
    jwt_algorithm:         str  = "HS256"
    jwt_access_token_ttl:  int  = 3600       # 1 hour
    jwt_refresh_token_ttl: int  = 604800     # 7 days

    # ── LLM providers ────────────────────────────────────────
    anthropic_api_key:    Optional[str] = None
    openai_api_key:       Optional[str] = None
    gemini_api_key:       Optional[str] = None
    openrouter_api_key:   Optional[str] = None
    ollama_base_url:      str = "http://localhost:11434"

    default_llm_provider: str = "anthropic"
    default_llm_model:    str = "claude-sonnet-4-6"

    # ── Repository Service (GitHub) ───────────────────────────
    github_token:         Optional[str] = None
    github_api_base_url:  str = "https://api.github.com"
    github_default_owner: Optional[str] = None   # org/user repos are created under
    repository_service_port: int = 8006
    qa_service_port:      int = 8008
    security_service_port: int = 8009
    devops_service_port: int = 8010

    # ── Monitoring Service (M3.7) ──────────────────────────────
    monitoring_service_port:            int = 8011
    monitoring_cycle_interval_seconds:  int = 30
    monitoring_incident_breach_cycles:  int = 3
    monitoring_alert_dedup_seconds:     int = 300

    # ── Incident Response Service (M3.8) ────────────────────────
    incident_response_service_port:        int = 8012
    incident_response_auto_rollback:       bool = True   # auto-trigger DevOps rollback for CRITICAL+ROLLBACK-classified incidents
    incident_response_recovery_timeout_seconds: int = 120

    # ── Platform Integration Service (M3.9) ─────────────────────
    integration_service_port: int = 8013

    # ── Storage ──────────────────────────────────────────────
    storage_backend:    str = "local"           # local | s3
    storage_local_path: str = "./data/artifacts"
    aws_s3_bucket:      Optional[str] = None
    aws_access_key_id:  Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_region:         str = "us-east-1"

    # ── Observability ────────────────────────────────────────
    log_level:  str = "INFO"
    log_format: str = "json"

    otel_exporter_otlp_endpoint: Optional[str] = None
    otel_service_name:           str = "aasc"

    # ── Validation ───────────────────────────────────────────
    @field_validator("database_url", mode="before")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        if not v:
            raise ValueError("DATABASE_URL is required")
        return v

    @field_validator("jwt_secret", mode="before")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if not v:
            raise ValueError("JWT_SECRET is required")
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def has_any_llm_provider(self) -> bool:
        return any([
            self.anthropic_api_key,
            self.openai_api_key,
            self.gemini_api_key,
            self.openrouter_api_key,
        ])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Returns the cached Settings singleton.
    Call get_settings() anywhere — it only parses the environment once.
    """
    return Settings()

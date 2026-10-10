"""Pytest configuration for AASC test suite."""
import os

import pytest

# core.config.settings.Settings has two *required* fields (DATABASE_URL,
# JWT_SECRET) and reads `.env`, which is (rightly) gitignored. On a developer
# machine the .env satisfies them; on a CI runner nothing does, so any test
# that reaches get_settings() failed with a ValidationError — or passed/failed
# depending on which test happened to populate the lru_cache first.
# setdefault() keeps a developer's real environment in charge when present.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-production-use")


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: full end-to-end tests requiring Docker stack")
    config.addinivalue_line("markers", "integration: integration tests requiring DB")
    config.addinivalue_line("markers", "unit: pure unit tests (no infrastructure)")

"""Pytest configuration for AASC test suite."""
import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: full end-to-end tests requiring Docker stack")
    config.addinivalue_line("markers", "integration: integration tests requiring DB")
    config.addinivalue_line("markers", "unit: pure unit tests (no infrastructure)")

"""
infrastructure/monitoring/telemetry.py
========================================
Sprint 3 — Monitoring Module.
Provides logger, metrics, and tracer to every service.
Call configure_telemetry() once at startup.
"""
from __future__ import annotations

from typing import Optional

import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

from core.config.settings import get_settings

# ── Prometheus metrics ────────────────────────────────────────

token_usage_total = Counter(
    "aasc_token_usage_total",
    "Total LLM tokens consumed",
    ["provider", "model", "department"],
)
token_cost_usd_total = Counter(
    "aasc_token_cost_usd_total",
    "Total LLM cost in USD",
    ["provider", "model", "department"],
)
workflow_phase_duration = Histogram(
    "aasc_workflow_phase_duration_seconds",
    "Time spent in each workflow phase",
    ["phase_name"],
    buckets=[10, 30, 60, 120, 300, 600, 1800, 3600],
)
agent_run_duration = Histogram(
    "aasc_agent_run_duration_seconds",
    "Agent execution time",
    ["agent_id", "department", "status"],
    buckets=[1, 5, 15, 30, 60, 120, 300],
)
active_projects = Gauge(
    "aasc_active_projects_total",
    "Number of projects currently running",
)
approval_wait_time = Histogram(
    "aasc_approval_wait_seconds",
    "Time from approval request to user response",
    ["artifact_type"],
    buckets=[60, 300, 900, 3600, 14400, 86400],
)
deployments_total = Counter(
    "aasc_deployments_total",
    "Total deployment attempts",
    ["status", "environment"],
)
ws_connections = Gauge(
    "aasc_websocket_connections",
    "Active WebSocket connections",
)
nats_messages_published = Counter(
    "aasc_nats_messages_published_total",
    "NATS messages published",
    ["subject_prefix"],
)

# ── Tracer ────────────────────────────────────────────────────

_tracer: Optional[trace.Tracer] = None


def get_tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        _tracer = trace.get_tracer("aasc")
    return _tracer


# ── Logging ───────────────────────────────────────────────────

def _configure_logging(log_level: str, log_format: str) -> None:
    import logging

    level = getattr(logging, log_level.upper(), logging.INFO)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
    ]

    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.BoundLogger,
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=level)


def _configure_tracing(endpoint: Optional[str], service_name: str) -> None:
    if not endpoint:
        return
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        resource  = Resource({SERVICE_NAME: service_name})
        provider  = TracerProvider(resource=resource)
        exporter  = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)
        trace.set_tracer_provider(provider)
    except ImportError:
        pass  # OTLP exporter not installed — skip silently


def configure_telemetry(metrics_port: int = 9100) -> None:
    """
    Call once at service startup.
    Configures logging, Prometheus metrics server, and OpenTelemetry tracing.
    """
    settings = get_settings()
    _configure_logging(settings.log_level, settings.log_format)
    _configure_tracing(settings.otel_exporter_otlp_endpoint, settings.otel_service_name)

    # Start Prometheus metrics endpoint on sidecar port
    try:
        start_http_server(metrics_port)
        structlog.get_logger().info("metrics_server_started", port=metrics_port)
    except OSError:
        pass  # Already started (e.g. in tests)

    structlog.get_logger().info(
        "telemetry_configured",
        log_level=settings.log_level,
        log_format=settings.log_format,
        otel_endpoint=settings.otel_exporter_otlp_endpoint or "disabled",
    )


# ── Metric helpers ────────────────────────────────────────────

def record_token_usage(
    provider:   str,
    model:      str,
    department: str,
    input_tok:  int,
    output_tok: int,
    cost_usd:   float,
) -> None:
    total = input_tok + output_tok
    token_usage_total.labels(provider=provider, model=model, department=department).inc(total)
    token_cost_usd_total.labels(provider=provider, model=model, department=department).inc(cost_usd)


def record_agent_run(agent_id: str, department: str, status: str, duration_s: float) -> None:
    agent_run_duration.labels(agent_id=agent_id, department=department, status=status)\
                      .observe(duration_s)

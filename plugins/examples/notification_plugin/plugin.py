"""
plugins/examples/notification_plugin/plugin.py
=================================
M4.10 §6 — demonstrates the `network`/`events` permissions with a
pluggable notification sink. The sink is injected via
`ctx.config.get("sink")` (a callable) rather than this module owning a
hardcoded webhook/SMTP integration — no assumed transport, same
"caller supplies the fetcher/connector" convention M4.9's
`backup_manager`/`environment_validator` both use. With no sink
configured, notifications are simply collected in `_sent` (in-memory)
so the plugin still runs standalone with zero external configuration.
"""
from __future__ import annotations

_sent = []


def _dispatch(ctx, payload):
    sink = None
    config = getattr(ctx, "config", None)
    if isinstance(config, dict):
        sink = config.get("sink")
    if callable(sink):
        sink(payload)
    else:
        _sent.append(payload)
    return payload


def incident_created(ctx, **kwargs):
    payload = {"event": "incident_created", "incident_id": kwargs.get("incident_id"),
               "severity": kwargs.get("severity", "unknown")}
    return _dispatch(ctx, payload)


def deployment_completed(ctx, **kwargs):
    payload = {"event": "deployment_completed", "version": kwargs.get("version"),
               "environment": kwargs.get("environment", "unknown")}
    return _dispatch(ctx, payload)

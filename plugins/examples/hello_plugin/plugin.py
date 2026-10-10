"""
plugins/examples/hello_plugin/plugin.py
=================================
M4.10 §6 — the minimal Plugin SDK example. No permissions requested, no
external I/O; demonstrates the three simplest hook names
(`startup`/`shutdown`/`after_workflow`) with the `def name(ctx, **kwargs)`
signature `plugin_runtime.dispatch_hook` expects.
"""
from __future__ import annotations


def startup(ctx, **kwargs):
    return {"message": "hello_plugin started", "plugin_id": "hello_plugin"}


def shutdown(ctx, **kwargs):
    return {"message": "hello_plugin stopped", "plugin_id": "hello_plugin"}


def after_workflow(ctx, **kwargs):
    workflow_name = kwargs.get("workflow_name", "unknown")
    return {"message": f"hello from hello_plugin — workflow '{workflow_name}' finished"}

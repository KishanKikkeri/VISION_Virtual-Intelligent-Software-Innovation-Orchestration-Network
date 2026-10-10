"""
examples/hello_world/main.py
=================================
M4.10 §5 — the smallest runnable example, wired against the real Plugin
SDK (M4.7): discovers `plugins/examples/hello_plugin`, imports its
entrypoint, and dispatches the `startup` and `after_workflow` hooks
through `plugin_runtime.dispatch_hook` — the exact same call path the
platform itself uses, not a stand-in.

Run: python examples/hello_world/main.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.integration.plugin_sdk.plugin_loader import discover_plugins, import_entrypoint
from services.integration.plugin_sdk.plugin_models import HookType, PluginRecord
from services.integration.plugin_sdk.plugin_runtime import PluginContext, dispatch_hook

PLUGINS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "examples"))


def main() -> int:
    discovered = discover_plugins(PLUGINS_DIR)
    hello = next((p for p in discovered if p.manifest.id == "hello_plugin"), None)
    if hello is None:
        print(f"hello_plugin not found under {PLUGINS_DIR}")
        return 1

    module = import_entrypoint(hello)
    record = PluginRecord(manifest=hello.manifest, source_path=hello.source_path)
    context = PluginContext()

    startup_results = dispatch_hook(HookType.STARTUP, [record], {"hello_plugin": module}, context)
    for r in startup_results:
        print(f"[{r.hook.value}] {r.plugin_id} -> {r.result}")

    finish_results = dispatch_hook(HookType.AFTER_WORKFLOW, [record], {"hello_plugin": module}, context,
                                    workflow_name="hello_world_demo")
    for r in finish_results:
        print(f"[{r.hook.value}] {r.plugin_id} -> {r.result}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

# hello_world

M4.10 §5 sample project — the smallest possible example that touches a
real platform component: it loads `plugins/examples/hello_plugin`
through the Plugin SDK's own manifest parser and runtime dispatcher
(`services.integration.plugin_sdk`), rather than hand-rolling a fake
plugin loader for the example. No database, no network, no Docker
required.

Run it:

```
python examples/hello_world/main.py
```

Expected output: the `startup` and `after_workflow` hook results printed
to stdout, sourced from `plugins/examples/hello_plugin/plugin.py`.

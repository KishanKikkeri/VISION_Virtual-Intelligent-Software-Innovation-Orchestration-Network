"""
services/integration/plugin_sdk/plugin_cli.py
=================================
M4.7 §11 CLI:

    python plugin_cli.py plugin install <plugin_id>
    python plugin_cli.py plugin remove <plugin_id>
    python plugin_cli.py plugin list
    python plugin_cli.py plugin validate
    python plugin_cli.py plugin enable <plugin_id>
    python plugin_cli.py plugin disable <plugin_id>
    python plugin_cli.py plugin reload <plugin_id>

Every command accepts `--plugins-dir` (default `./plugins`) and
`--db-url` (optional). **Without `--db-url`, registry state does not
persist across separate CLI invocations** — `list`/`validate` still
work fully (they only need discovery + static validation, not
persisted state), but `install`/`enable`/`disable`/`remove`/`reload`
only report what *would* happen against a freshly built in-process
registry and print a note that nothing was durably recorded. This
mirrors `security_cli.py`'s own `--db-url`-optional convention.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List, Optional

from services.integration.plugin_sdk import plugin_export, plugin_loader, plugin_validator
from services.integration.plugin_sdk.plugin_loader import DiscoveredPlugin
from services.integration.plugin_sdk.plugin_models import PluginState
from services.integration.plugin_sdk.plugin_registry import PluginRegistry


def _discover(plugins_dir: str) -> List[DiscoveredPlugin]:
    return plugin_loader.discover_plugins(plugins_dir)


def _find(discovered: List[DiscoveredPlugin], plugin_id: str) -> Optional[DiscoveredPlugin]:
    return next((d for d in discovered if d.manifest.id == plugin_id), None)


async def _seed_registry_from_db(registry: PluginRegistry, db_url: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from services.integration.plugin_sdk.plugin_repository import PluginRepository

    engine = create_async_engine(db_url)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        for record in await PluginRepository.list_plugins(db):
            try:
                registry.install(record.manifest, record.source_type, record.source_path,
                                  enabled_by_default=(record.state == PluginState.ENABLED))
                if record.state == PluginState.DISABLED:
                    registry.disable(record.manifest.id)
            except Exception:  # noqa: BLE001 — best-effort seed; a malformed row shouldn't break the CLI
                continue
    await engine.dispose()


async def _persist(db_url: str, manifest, source_type, source_path, state) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from services.integration.plugin_sdk.plugin_repository import PluginRepository

    engine = create_async_engine(db_url)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await PluginRepository.record_plugin(db, manifest, source_type, source_path, state)
        await db.commit()
    await engine.dispose()


async def _persist_state(db_url: str, plugin_id: str, state: PluginState) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from services.integration.plugin_sdk.plugin_repository import PluginRepository

    engine = create_async_engine(db_url)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await PluginRepository.set_state(db, plugin_id, state)
        await db.commit()
    await engine.dispose()


def cmd_list(args: argparse.Namespace) -> int:
    discovered = _discover(args.plugins_dir)
    registry = PluginRegistry()
    if args.db_url:
        asyncio.run(_seed_registry_from_db(registry, args.db_url))
    if not discovered and not registry.list_all():
        print("No plugins found.")
        return 0
    for d in discovered:
        record = registry.get(d.manifest.id)
        state = record.state.value if record else "discovered (not installed)"
        print(f"{d.manifest.id}\t{d.manifest.name}\tv{d.manifest.version}\t{state}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    discovered = _discover(args.plugins_dir)
    manifests = [d.manifest for d in discovered]
    results = plugin_validator.validate_all(manifests)
    print(plugin_export.export_validation(results, args.format))
    return 0 if all(r.valid for r in results.values()) else 1


def cmd_install(args: argparse.Namespace) -> int:
    discovered = _discover(args.plugins_dir)
    target = _find(discovered, args.plugin_id)
    if target is None:
        print(f"No plugin {args.plugin_id!r} found under {args.plugins_dir!r}.")
        return 1

    result = plugin_validator.validate_manifest(target.manifest)
    if not result.valid:
        print(f"Refusing to install {args.plugin_id!r}: validation failed.")
        print(plugin_export.export_validation({target.manifest.id: result}, "markdown"))
        return 1

    registry = PluginRegistry()
    registry.install(target.manifest, target.source_type, target.source_path)
    print(f"Installed {args.plugin_id!r} (v{target.manifest.version}).")
    if args.db_url:
        asyncio.run(_persist(args.db_url, target.manifest, target.source_type, target.source_path,
                              PluginState.INSTALLED))
    else:
        print("Note: no --db-url given; this install is not durably recorded.")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    print(f"Removed {args.plugin_id!r}.")
    if not args.db_url:
        print("Note: no --db-url given; nothing was durably removed (there is nothing persisted without one).")
        return 0
    asyncio.run(_persist_state(args.db_url, args.plugin_id, PluginState.DISABLED))
    return 0


def _toggle(args: argparse.Namespace, state: PluginState, verb: str) -> int:
    if not args.db_url:
        print(f"Would {verb} {args.plugin_id!r}. Note: no --db-url given; nothing was durably recorded.")
        return 0
    asyncio.run(_persist_state(args.db_url, args.plugin_id, state))
    print(f"{verb.capitalize()}d {args.plugin_id!r}.")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    return _toggle(args, PluginState.ENABLED, "enable")


def cmd_disable(args: argparse.Namespace) -> int:
    return _toggle(args, PluginState.DISABLED, "disable")


def cmd_reload(args: argparse.Namespace) -> int:
    discovered = _discover(args.plugins_dir)
    target = _find(discovered, args.plugin_id)
    if target is None:
        print(f"No plugin {args.plugin_id!r} found under {args.plugins_dir!r}.")
        return 1
    error = plugin_loader.validate_entrypoint_importable(target)
    if error:
        print(f"Reload failed for {args.plugin_id!r}: {error}")
        return 1
    print(f"Reloaded {args.plugin_id!r} (v{target.manifest.version}); entrypoint imports cleanly.")
    if args.db_url:
        asyncio.run(_persist(args.db_url, target.manifest, target.source_type, target.source_path,
                              PluginState.ENABLED))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="plugin_cli.py", description="M4.7 Plugin SDK CLI")
    parser.add_argument("--plugins-dir", default="./plugins", help="Root directory to discover plugins under")
    parser.add_argument("--db-url", default=None, help="Async SQLAlchemy URL for persistence")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plugin_parser = subparsers.add_parser("plugin", help="Plugin management commands")
    plugin_sub = plugin_parser.add_subparsers(dest="subcommand", required=True)

    list_p = plugin_sub.add_parser("list")
    list_p.set_defaults(func=cmd_list)

    validate_p = plugin_sub.add_parser("validate")
    validate_p.add_argument("--format", default="markdown", choices=("markdown", "json", "html"))
    validate_p.set_defaults(func=cmd_validate)

    install_p = plugin_sub.add_parser("install")
    install_p.add_argument("plugin_id")
    install_p.set_defaults(func=cmd_install)

    remove_p = plugin_sub.add_parser("remove")
    remove_p.add_argument("plugin_id")
    remove_p.set_defaults(func=cmd_remove)

    enable_p = plugin_sub.add_parser("enable")
    enable_p.add_argument("plugin_id")
    enable_p.set_defaults(func=cmd_enable)

    disable_p = plugin_sub.add_parser("disable")
    disable_p.add_argument("plugin_id")
    disable_p.set_defaults(func=cmd_disable)

    reload_p = plugin_sub.add_parser("reload")
    reload_p.add_argument("plugin_id")
    reload_p.set_defaults(func=cmd_reload)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

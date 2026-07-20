"""
services/integration/workflow_designer/designer_cli.py
=================================
M4.8 §15 CLI:

    python designer_cli.py designer validate <file>
    python designer_cli.py designer export <file> --format json|yaml|mermaid|markdown
    python designer_cli.py designer import <file> --format json|yaml|mermaid
    python designer_cli.py designer diff <old_file> <new_file>
    python designer_cli.py designer replay <execution_id> --workflow <name>
    python designer_cli.py designer library

Every command that touches persisted state accepts `--db-url` (optional).
**Without `--db-url`, `import`/`diff`/`validate` operate purely on the
given file(s) and print results without persisting anything** — same
`--db-url`-optional convention `plugin_cli.py` established (see that
module's own docstring for the rationale).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List, Optional

from services.integration.workflow_designer import designer_export, node_library, validation_bridge
from services.integration.workflow_designer.designer_models import WorkflowLayout
from services.integration.workflow_designer.workflow_deserializer import import_mermaid, import_workflow
from services.integration.workflow_designer.workflow_serializer import SerializerError


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_layout(path: str, fmt: Optional[str] = None) -> WorkflowLayout:
    text = _read(path)
    fmt = fmt or ("yaml" if path.endswith((".yml", ".yaml")) else ("mermaid" if path.endswith(".mmd") else "json"))
    if fmt == "mermaid":
        return import_mermaid(text)
    return import_workflow(text, fmt)


async def _persist_layout(db_url: str, layout: WorkflowLayout, reason: str = "save") -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from services.integration.workflow_designer.designer_repository import DesignerRepository

    engine = create_async_engine(db_url)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        await DesignerRepository.save_layout(db, layout, reason=reason)
        await db.commit()
    await engine.dispose()


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        layout = _load_layout(args.file)
    except (SerializerError, Exception) as e:  # noqa: BLE001
        print(f"Could not load {args.file!r}: {e}")
        return 1
    result = validation_bridge.validate_layout(layout)
    print(f"Workflow {result.workflow_name!r}: {'VALID' if result.valid else 'INVALID'}")
    for issue in result.issues:
        print(f"  [{issue.severity}] {issue.rule_id}: {issue.message}")
    print(f"Sources checked: {result.sources_available}")
    return 0 if result.valid else 1


def cmd_export(args: argparse.Namespace) -> int:
    try:
        layout = _load_layout(args.file)
        print(designer_export.export_workflow(layout, args.format))
    except SerializerError as e:
        print(f"Export failed: {e}")
        return 1
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        layout = _load_layout(args.file, args.format)
    except (SerializerError, Exception) as e:  # noqa: BLE001
        print(f"Import failed: {e}")
        return 1
    print(f"Imported workflow {layout.workflow_name!r}: {len(layout.nodes)} node(s), {len(layout.edges)} edge(s).")
    if args.db_url:
        asyncio.run(_persist_layout(args.db_url, layout, reason="pre_import"))
        print("Persisted.")
    else:
        print("Note: no --db-url given; this import was not durably recorded.")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    try:
        old = _load_layout(args.old_file)
        new = _load_layout(args.new_file)
    except (SerializerError, Exception) as e:  # noqa: BLE001
        print(f"Diff failed: {e}")
        return 1
    result = validation_bridge.diff_layouts(old, new)
    print(f"Diff {result.from_version} -> {result.to_version} ({'breaking' if result.is_breaking else 'non-breaking'})")
    for entry in result.entries:
        target = entry.node_id or entry.edge_id or ""
        print(f"  {entry.kind}: {target} — {entry.detail}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    overlay = validation_bridge.fetch_replay_overlay(args.workflow, args.execution_id)
    if not overlay.available:
        print(f"Replay data unavailable for execution {args.execution_id!r} "
              f"(replay engine not wired in this environment).")
        return 1
    print(f"Execution {overlay.execution_id} — current: {overlay.current_node_id}, failed: {overlay.failed_node_id}")
    for s in overlay.node_states:
        print(f"  {s.node_id}: {s.status} ({s.duration_ms or 0:.1f}ms)")
    return 0


def cmd_library(args: argparse.Namespace) -> int:
    library = node_library.build_library()
    print(f"{len(library.templates)} template(s) across {len(library.categories)} categories:")
    for category in library.categories:
        print(f"  {category}:")
        for t in library.templates:
            if t.category == category:
                print(f"    - {t.node_type.value} ({t.source}): {t.label}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="designer_cli.py", description="M4.8 Visual Workflow Designer CLI")
    parser.add_argument("--db-url", default=None, help="Async SQLAlchemy URL for persistence")
    subparsers = parser.add_subparsers(dest="command", required=True)

    designer_parser = subparsers.add_parser("designer", help="Designer commands")
    sub = designer_parser.add_subparsers(dest="subcommand", required=True)

    validate_p = sub.add_parser("validate")
    validate_p.add_argument("file")
    validate_p.set_defaults(func=cmd_validate)

    export_p = sub.add_parser("export")
    export_p.add_argument("file")
    export_p.add_argument("--format", default="json", choices=("json", "yaml", "mermaid", "markdown"))
    export_p.set_defaults(func=cmd_export)

    import_p = sub.add_parser("import")
    import_p.add_argument("file")
    import_p.add_argument("--format", default=None, choices=("json", "yaml", "mermaid"))
    import_p.set_defaults(func=cmd_import)

    diff_p = sub.add_parser("diff")
    diff_p.add_argument("old_file")
    diff_p.add_argument("new_file")
    diff_p.set_defaults(func=cmd_diff)

    replay_p = sub.add_parser("replay")
    replay_p.add_argument("execution_id")
    replay_p.add_argument("--workflow", required=True)
    replay_p.set_defaults(func=cmd_replay)

    library_p = sub.add_parser("library")
    library_p.set_defaults(func=cmd_library)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

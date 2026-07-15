"""
infrastructure/storage/base.py + local.py
==========================================
Sprint 3 — Storage Module.
Pluggable artifact storage. V1 uses local filesystem.
V2 adds S3 via the same interface — zero changes to callers.
"""
from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional, Union

import structlog

log = structlog.get_logger(__name__)


# ── Base interface ────────────────────────────────────────────

class ArtifactStorage(ABC):
    """
    Storage abstraction for artifact content.
    All artifact reads/writes must go through this interface.
    """

    @abstractmethod
    async def store(
        self,
        project_id:    str,
        artifact_type: str,
        version:       int,
        content:       Union[str, bytes, dict],
        extension:     str = "json",
    ) -> str:
        """
        Stores artifact content and returns a storage_ref string.
        storage_ref is stored in the artifacts table and used to retrieve content.
        """
        ...

    @abstractmethod
    async def load(self, storage_ref: str) -> Optional[Union[str, bytes, dict]]:
        """Retrieves artifact content by storage_ref. Returns None if not found."""
        ...

    @abstractmethod
    async def delete(self, storage_ref: str) -> bool:
        """Deletes artifact content. Returns True if deleted, False if not found."""
        ...

    @abstractmethod
    async def exists(self, storage_ref: str) -> bool:
        """Returns True if the storage_ref points to existing content."""
        ...


# ── Local filesystem implementation ──────────────────────────

class LocalArtifactStorage(ArtifactStorage):
    """
    Stores artifacts as files on the local filesystem.
    Directory structure: {base_path}/{project_id}/{artifact_type}/v{version}.{ext}
    """

    def __init__(self, base_path: str = "./data/artifacts") -> None:
        self._base = Path(base_path)
        self._base.mkdir(parents=True, exist_ok=True)
        log.info("local_storage_init", path=str(self._base))

    async def store(
        self,
        project_id:    str,
        artifact_type: str,
        version:       int,
        content:       Union[str, bytes, dict],
        extension:     str = "json",
    ) -> str:
        dir_path = self._base / project_id / artifact_type
        dir_path.mkdir(parents=True, exist_ok=True)

        filename   = f"v{version}.{extension}"
        file_path  = dir_path / filename
        storage_ref = f"local://{project_id}/{artifact_type}/{filename}"

        if isinstance(content, dict):
            file_path.write_text(json.dumps(content, indent=2, default=str))
        elif isinstance(content, bytes):
            file_path.write_bytes(content)
        else:
            file_path.write_text(str(content))

        log.debug("artifact_stored", ref=storage_ref, size=file_path.stat().st_size)
        return storage_ref

    async def load(self, storage_ref: str) -> Optional[Union[str, dict]]:
        file_path = self._resolve(storage_ref)
        if not file_path or not file_path.exists():
            return None
        content = file_path.read_text()
        if storage_ref.endswith(".json"):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content
        return content

    async def delete(self, storage_ref: str) -> bool:
        file_path = self._resolve(storage_ref)
        if not file_path or not file_path.exists():
            return False
        file_path.unlink()
        return True

    async def exists(self, storage_ref: str) -> bool:
        file_path = self._resolve(storage_ref)
        return file_path is not None and file_path.exists()

    def _resolve(self, storage_ref: str) -> Optional[Path]:
        """Converts a storage_ref back to a filesystem path."""
        if not storage_ref.startswith("local://"):
            return None
        relative = storage_ref[len("local://"):]
        return self._base / relative


# ── Factory ───────────────────────────────────────────────────

_storage: Optional[ArtifactStorage] = None


def init_storage(backend: str = "local", **kwargs) -> ArtifactStorage:
    """
    Initialises the global storage backend. Call once at startup.

    backend = "local"  → LocalArtifactStorage
    backend = "s3"     → S3ArtifactStorage (Phase 2)
    """
    global _storage
    if backend == "local":
        _storage = LocalArtifactStorage(**kwargs)
    else:
        raise ValueError(f"Unknown storage backend: '{backend}'. Use 'local' or 's3'.")
    return _storage


def get_storage() -> ArtifactStorage:
    """Returns the global storage instance. Raises if not initialised."""
    if _storage is None:
        raise RuntimeError("Storage not initialised. Call init_storage() first.")
    return _storage

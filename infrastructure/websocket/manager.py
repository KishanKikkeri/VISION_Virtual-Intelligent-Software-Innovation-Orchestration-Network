"""
infrastructure/websocket/manager.py
=====================================
Sprint 3 — WebSocket Module.
Manages active WebSocket connections per project.
Broadcasts real-time events to all connected clients for a project.

Connection lifecycle:
  1. Client connects:  WS /ws/projects/{project_id}
  2. Manager registers the connection.
  3. Any service calls broadcast(project_id, event) to push updates.
  4. Client disconnects: Manager unregisters.

All agent events that affect the UI are routed through this manager
after being received from NATS.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import structlog
from fastapi import WebSocket, WebSocketDisconnect

log = structlog.get_logger(__name__)


class WebSocketManager:
    """
    Thread-safe WebSocket connection registry.
    One instance shared across all routes (module-level singleton).
    """

    def __init__(self) -> None:
        # project_id → set of active WebSocket connections
        self._connections: Dict[str, Set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    # ── Connection management ──────────────────────────────────

    async def connect(self, project_id: str, websocket: WebSocket) -> None:
        """Accepts a new WebSocket connection and registers it."""
        await websocket.accept()
        async with self._lock:
            self._connections[project_id].add(websocket)
        log.info("ws_connected", project_id=project_id,
                 total=len(self._connections[project_id]))

        # Send a welcome message immediately
        await self._send_to(websocket, {
            "type":       "connected",
            "project_id": project_id,
            "message":    "Connected to AASC real-time stream",
            "timestamp":  datetime.utcnow().isoformat(),
        })

    async def disconnect(self, project_id: str, websocket: WebSocket) -> None:
        """Removes a WebSocket from the registry."""
        async with self._lock:
            self._connections[project_id].discard(websocket)
            if not self._connections[project_id]:
                del self._connections[project_id]
        log.info("ws_disconnected", project_id=project_id)

    # ── Broadcasting ──────────────────────────────────────────

    async def broadcast(
        self,
        project_id: str,
        event_type: str,
        payload:    Dict[str, Any],
    ) -> int:
        """
        Broadcasts an event to all connected clients for a project.
        Returns the number of clients the message was sent to.
        Dead connections are automatically pruned.
        """
        message = {
            "type":       event_type,
            "project_id": project_id,
            "payload":    payload,
            "timestamp":  datetime.utcnow().isoformat(),
        }

        connections = set(self._connections.get(project_id, set()))
        if not connections:
            log.debug("ws_no_listeners", project_id=project_id, event_type=event_type)
            return 0

        dead: List[WebSocket] = []
        sent = 0
        for ws in connections:
            try:
                await self._send_to(ws, message)
                sent += 1
            except Exception:
                dead.append(ws)

        # Prune dead connections
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections[project_id].discard(ws)

        log.debug("ws_broadcast", project_id=project_id,
                  event_type=event_type, sent=sent, pruned=len(dead))
        return sent

    async def broadcast_system(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Broadcasts to ALL connected clients across all projects."""
        all_projects = list(self._connections.keys())
        for pid in all_projects:
            await self.broadcast(pid, event_type, payload)

    # ── Connection serving ────────────────────────────────────

    async def serve(
        self,
        project_id: str,
        websocket:  WebSocket,
        on_message: Optional[Any] = None,
    ) -> None:
        """
        Full connection lifecycle handler. Call from route handler.
        Handles connect, message loop, disconnect, and cleanup.

        Usage in FastAPI route:
            @router.websocket("/ws/projects/{project_id}")
            async def ws_endpoint(project_id: str, ws: WebSocket):
                await ws_manager.serve(project_id, ws)
        """
        await self.connect(project_id, websocket)
        try:
            while True:
                # Keep connection alive; handle incoming messages if handler provided
                data = await websocket.receive_text()
                if on_message:
                    try:
                        msg = json.loads(data)
                        await on_message(project_id, msg)
                    except json.JSONDecodeError:
                        pass
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.warning("ws_error", project_id=project_id, error=str(exc))
        finally:
            await self.disconnect(project_id, websocket)

    # ── Ping / keepalive ──────────────────────────────────────

    async def ping_all(self) -> None:
        """Sends a ping to all connections to detect dead ones. Call periodically."""
        for pid, connections in list(self._connections.items()):
            for ws in list(connections):
                try:
                    await ws.send_text('{"type":"ping"}')
                except Exception:
                    await self.disconnect(pid, ws)

    # ── Stats ─────────────────────────────────────────────────

    @property
    def connection_count(self) -> int:
        return sum(len(v) for v in self._connections.values())

    @property
    def project_count(self) -> int:
        return len(self._connections)

    # ── Internal ──────────────────────────────────────────────

    @staticmethod
    async def _send_to(ws: WebSocket, message: Dict[str, Any]) -> None:
        await ws.send_text(json.dumps(message, default=str))


# ── Module-level singleton ────────────────────────────────────

ws_manager = WebSocketManager()

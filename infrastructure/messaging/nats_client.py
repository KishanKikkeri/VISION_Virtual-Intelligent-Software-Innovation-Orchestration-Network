"""
infrastructure/messaging/nats_client.py
========================================
Sprint 3 — Messaging Module.
Wraps nats-py with JetStream support.
All agent events and workflow transitions route through this client.

Stream configuration:
  AASC_EVENTS  — all platform events (subjects: *.*.*)
  AASC_DLQ     — dead letter queue (subjects: dlq.*)

Usage:
  client = NATSClient()
  await client.connect(settings.nats_url)
  await client.publish("product.requirements.completed", {"project_id": "..."})
  await client.subscribe("manager.>", handler)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

import nats
import nats.js
import structlog
from nats.aio.client import Client as NATSConn
from nats.js.api import StreamConfig, RetentionPolicy, StorageType

log = structlog.get_logger(__name__)

# JetStream stream definitions
STREAM_CONFIGS = [
    StreamConfig(
        name="AASC_EVENTS",
        subjects=["manager.>", "product.>", "architecture.>",
                  "engineering.>", "repository.>", "qa.>", "security.>",
                  "devops.>", "docs.>", "token_ledger.>",
                  "monitoring.>"],  # M3.7 — added, additive only
        retention=RetentionPolicy.WORK_QUEUE,
        storage=StorageType.FILE,
        max_age=7 * 24 * 3600,   # 7 days
        max_msgs=1_000_000,
        max_bytes=1 * 1024 ** 3, # 1 GB
        num_replicas=1,           # increase to 3 in production
    ),
    StreamConfig(
        name="AASC_DLQ",
        subjects=["dlq.>"],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_age=30 * 24 * 3600,  # 30 days
        num_replicas=1,
    ),
]


class NATSClient:
    """
    Async NATS JetStream client.
    One instance per service, created at startup via init_nats().
    """

    def __init__(self) -> None:
        self._nc: Optional[NATSConn] = None
        self._js: Optional[nats.js.JetStreamContext] = None
        self._subscriptions: List[Any] = []

    # ── Lifecycle ─────────────────────────────────────────────

    async def connect(self, url: str) -> None:
        self._nc = await nats.connect(
            url,
            error_cb=self._on_error,
            closed_cb=self._on_closed,
            reconnected_cb=self._on_reconnected,
            max_reconnect_attempts=10,
            reconnect_time_wait=2,
        )
        self._js = self._nc.jetstream()
        await self._ensure_streams()
        log.info("nats_connected", url=url)

    async def drain(self) -> None:
        """Graceful shutdown — flush and drain all subscriptions."""
        if self._nc and not self._nc.is_closed:
            await self._nc.drain()
            log.info("nats_drained")

    # ── Publishing ────────────────────────────────────────────

    async def publish(
        self,
        subject: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Publishes a message to a JetStream subject.
        Payload is serialised to JSON bytes.
        """
        if not self._js:
            raise RuntimeError("NATS not connected. Call connect() first.")
        data = json.dumps(payload, default=str).encode()
        ack = await self._js.publish(subject, data, headers=headers)
        log.debug("nats_published", subject=subject, seq=ack.seq)

    async def publish_core(
        self,
        subject: str,
        payload: Dict[str, Any],
    ) -> None:
        """
        Core NATS publish (no JetStream — for fire-and-forget).
        Used for WebSocket relay events.
        """
        if not self._nc:
            raise RuntimeError("NATS not connected.")
        data = json.dumps(payload, default=str).encode()
        await self._nc.publish(subject, data)

    # ── Subscribing ───────────────────────────────────────────

    async def subscribe(
        self,
        subject:  str,
        handler:  Callable[[Dict[str, Any]], Coroutine],
        queue:    Optional[str] = None,
        durable:  Optional[str] = None,
    ) -> None:
        """
        JetStream push subscription.
        handler receives the decoded JSON payload dict.
        durable: set a durable consumer name for restart-safe delivery.
        """
        if not self._js:
            raise RuntimeError("NATS not connected.")

        async def _message_handler(msg):
            try:
                payload = json.loads(msg.data.decode())
                await handler(payload)
                await msg.ack()
            except Exception as exc:
                log.error("nats_handler_error", subject=msg.subject, error=str(exc))
                await msg.nak()   # re-deliver

        if durable:
            sub = await self._js.subscribe(subject, durable=durable,
                                           queue=queue, cb=_message_handler)
        else:
            sub = await self._js.subscribe(subject, queue=queue, cb=_message_handler)

        self._subscriptions.append(sub)
        log.info("nats_subscribed", subject=subject, durable=durable)

    async def subscribe_core(
        self,
        subject: str,
        handler: Callable[[Dict[str, Any]], Coroutine],
    ) -> None:
        """Core NATS subscription (no JetStream). For ephemeral listeners."""
        if not self._nc:
            raise RuntimeError("NATS not connected.")

        async def _msg_handler(msg):
            try:
                payload = json.loads(msg.data.decode())
                await handler(payload)
            except Exception as exc:
                log.error("core_handler_error", subject=msg.subject, error=str(exc))

        sub = await self._nc.subscribe(subject, cb=_msg_handler)
        self._subscriptions.append(sub)

    # ── Request / Reply ───────────────────────────────────────

    async def request(
        self,
        subject: str,
        payload: Dict[str, Any],
        timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Synchronous request-reply over NATS core.
        Times out after `timeout` seconds.
        """
        if not self._nc:
            raise RuntimeError("NATS not connected.")
        data = json.dumps(payload, default=str).encode()
        response = await self._nc.request(subject, data, timeout=timeout)
        return json.loads(response.data.decode())

    # ── Health ────────────────────────────────────────────────

    async def check_health(self) -> bool:
        """Returns True if the NATS connection is alive."""
        if not self._nc:
            return False
        return self._nc.is_connected

    # ── Internal ──────────────────────────────────────────────

    async def _ensure_streams(self) -> None:
        """Creates or updates JetStream streams on first connection."""
        if not self._js:
            return
        jsm = await self._nc.jetstream()
        for cfg in STREAM_CONFIGS:
            try:
                await jsm.find_stream(cfg.subjects[0])
                log.debug("nats_stream_exists", stream=cfg.name)
            except nats.js.errors.NotFoundError:
                await jsm.add_stream(cfg)
                log.info("nats_stream_created", stream=cfg.name)

    async def _on_error(self, exc: Exception) -> None:
        log.error("nats_error", error=str(exc))

    async def _on_closed(self) -> None:
        log.warning("nats_connection_closed")

    async def _on_reconnected(self) -> None:
        log.info("nats_reconnected")


# ── Module-level singleton ────────────────────────────────────

_client: Optional[NATSClient] = None


async def init_nats(url: str) -> NATSClient:
    """Initialises the global NATS client. Call once at startup."""
    global _client
    _client = NATSClient()
    await _client.connect(url)
    return _client


def get_nats() -> NATSClient:
    """Returns the global NATS client. Raises if not initialised."""
    if _client is None:
        raise RuntimeError("NATS not initialised. Call init_nats() first.")
    return _client

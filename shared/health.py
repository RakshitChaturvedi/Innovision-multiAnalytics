"""Minimal /health HTTP endpoint (asyncio streams only, no new dependencies).

GET /health -> 200 with JSON, or 503 when Redis or the database is
unreachable or a consumer task has died. Anything else -> 404.

`seconds_since_last_message` is informational only: an idle stream is not
unhealthy, so it never turns the answer into 503.
"""
import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shared.service_settings import ServiceSettings

logger = logging.getLogger(__name__)

PING_TIMEOUT_S = 2.0

Check = Callable[[], Awaitable[None]]


def redis_check(redis_url: str) -> tuple[Check, Callable[[], Awaitable[None]]]:
    client = aioredis.from_url(redis_url, socket_connect_timeout=PING_TIMEOUT_S,
                               socket_timeout=PING_TIMEOUT_S)

    async def check() -> None:
        await client.ping()

    return check, client.aclose


def db_check(database_url: str) -> tuple[Check, Callable[[], Awaitable[None]]]:
    engine: AsyncEngine = create_async_engine(database_url, pool_size=1, max_overflow=0,
                                              pool_pre_ping=True)

    async def check() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    return check, engine.dispose


class HealthServer:
    def __init__(
        self,
        service: str,
        consumers: list,
        tasks: Iterable[asyncio.Task],
        *,
        port: int,
        host: str = "0.0.0.0",
        redis_check: Check,
        db_check: Check,
    ):
        self.service = service
        self.consumers = consumers
        self.tasks = list(tasks)
        self.port = port
        self.host = host
        self._redis_check = redis_check
        self._db_check = db_check
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("health_server_listening service=%s port=%d", self.service, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @staticmethod
    async def _ping(check: Check) -> dict[str, Any]:
        try:
            await asyncio.wait_for(check(), timeout=PING_TIMEOUT_S)
            return {"ok": True}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # reported in the body and as 503
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}

    def _dead_tasks(self) -> list[str]:
        dead = []
        for t in self.tasks:
            if t.done():
                if t.cancelled():
                    dead.append(f"{t.get_name()}: cancelled")
                else:
                    exc = t.exception()
                    dead.append(f"{t.get_name()}: {exc!r}" if exc else f"{t.get_name()}: exited")
        return dead

    async def report(self) -> tuple[int, dict[str, Any]]:
        redis_res, db_res = await asyncio.gather(
            self._ping(self._redis_check), self._ping(self._db_check)
        )
        now = time.monotonic()
        consumers = []
        for c in self.consumers:
            last = getattr(c, "last_processed_monotonic", None)
            consumers.append({
                **c.stats(),
                "seconds_since_last_message": None if last is None else round(now - last, 3),
            })
        known = [c["seconds_since_last_message"] for c in consumers
                 if c["seconds_since_last_message"] is not None]
        dead = self._dead_tasks()
        healthy = redis_res["ok"] and db_res["ok"] and not dead
        body = {
            "service": self.service,
            "status": "ok" if healthy else "unhealthy",
            "redis": redis_res,
            "db": db_res,
            "consumers_alive": not dead,
            "dead_tasks": dead,
            "seconds_since_last_message": min(known) if known else None,
            "consumers": consumers,
        }
        return (200 if healthy else 503), body

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            # drain headers
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1").split()
            if len(parts) >= 2 and parts[0] == "GET" and parts[1].split("?")[0] == "/health":
                status, body = await self.report()
                if status != 200:
                    logger.warning("health_unhealthy service=%s body=%s", self.service, body)
            else:
                status, body = 404, {"error": "not found"}
            payload = json.dumps(body, default=str).encode()
            reason = {200: "OK", 404: "Not Found", 503: "Service Unavailable"}[status]
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload
            )
            await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("health_request_failed service=%s", self.service, exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                logger.debug("health_client_close_failed", exc_info=True)


class HealthSettings(ServiceSettings):
    HEALTH_PORT: int | None = None


def health_port(default: int) -> int:
    """HEALTH_PORT from env/.env, else the service's default."""
    port = HealthSettings().HEALTH_PORT
    return default if port is None else port


async def start_health_server(
    service: str,
    consumers: list,
    tasks: Iterable[asyncio.Task],
    port: int,
    *,
    redis_url: str | None = None,
    database_url: str | None = None,
) -> tuple[HealthServer, Callable[[], Awaitable[None]]]:
    """Build with real Redis/DB checks (shared settings unless the service
    passes its own URLs), start, and return (server, close) where close()
    stops the server and releases the clients."""
    from shared.config import settings

    r_check, r_close = redis_check(redis_url or settings.redis_url())
    d_check, d_close = db_check(database_url or settings.DATABASE_URL)
    server = HealthServer(service, consumers, tasks, port=port,
                          redis_check=r_check, db_check=d_check)
    await server.start()

    async def close() -> None:
        await server.stop()
        await r_close()
        await d_close()

    return server, close

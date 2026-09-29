"""Mock platform Alert Management: consume alerts:live and validate each alert
with the platform AlertEvent model + AlertEventValidator.

    python -m tools.mock_platform.alert_sink --camera-id <uuid> [--camera-id ...]

Group `mock_alert_sink`. Every entry is counted as valid / invalid / duplicate
(same alert_id seen before: the platform dedupes these) and then XACKed.
On exit (Ctrl-C, or --idle-exit S seconds without alerts) a summary is printed;
the exit code is 1 if any alert was invalid.
"""
import argparse
import asyncio
import json
import logging
from dataclasses import dataclass, field
from uuid import UUID

from pydantic import ValidationError
from redis.exceptions import ResponseError

from shared.platform_contracts.alert_event import AlertEvent, AlertEventValidator

logger = logging.getLogger(__name__)

STREAM = "alerts:live"
GROUP = "mock_alert_sink"


@dataclass
class AlertSink:
    redis: object
    known_cam_ids: set[UUID]
    consumer: str = "sink-1"
    valid: int = 0
    invalid: int = 0
    duplicates: int = 0
    seen: set[UUID] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def check(self, msg_id: str, data: dict) -> None:
        raw = data.get(b"data") or data.get("data")
        try:
            if raw is None:
                raise ValueError("entry has no 'data' field")
            alert = AlertEvent.model_validate_json(raw)
            problems = AlertEventValidator.validate(alert, self.known_cam_ids)
        except (ValidationError, ValueError) as exc:
            problems, alert = [str(exc)], None
        if problems:
            self.invalid += 1
            self.errors.append(f"{msg_id}: {'; '.join(problems)}")
            logger.error("invalid_alert msg=%s problems=%s", msg_id, problems)
            return
        if alert.alert_id in self.seen:
            self.duplicates += 1
            logger.warning("duplicate_alert alert_id=%s msg=%s", alert.alert_id, msg_id)
            return
        self.seen.add(alert.alert_id)
        self.valid += 1
        logger.info("alert %s %s %s cam=%s", alert.alert_type, alert.severity.value,
                    alert.title, alert.camera_id)

    async def poll(self, block_ms: int = 1000) -> int:
        """Read once (own pending first, then new). Returns entries handled."""
        handled = 0
        for start in ("0", ">"):
            resp = await self.redis.xreadgroup(
                GROUP, self.consumer, {STREAM: start}, count=100,
                block=None if start == "0" else block_ms,
            )
            for _stream, entries in resp or []:
                for msg_id, data in entries:
                    msg_id = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                    self.check(msg_id, data)
                    await self.redis.xack(STREAM, GROUP, msg_id)
                    handled += 1
        return handled

    def summary(self) -> dict:
        return {"valid": self.valid, "invalid": self.invalid,
                "duplicates": self.duplicates, "errors": self.errors[:20]}


async def run(args) -> int:
    import redis.asyncio as aioredis

    from shared.config import settings

    redis = aioredis.from_url(args.redis_url or settings.redis_url())
    sink = AlertSink(redis, {UUID(c) for c in args.camera_id})
    await sink.ensure_group()
    idle = 0.0
    try:
        while not args.idle_exit or idle < args.idle_exit:
            idle = 0.0 if await sink.poll() else idle + 1.0
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await redis.aclose()
        print(json.dumps(sink.summary(), indent=2))
    return 1 if sink.invalid else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera-id", action="append", required=True,
                    help="registered camera ids (AlertEventValidator rejects others)")
    ap.add_argument("--idle-exit", type=float, default=0, help="exit after S idle seconds")
    ap.add_argument("--redis-url", help="default: REDIS_HOST/PORT/DB from env")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        code = asyncio.run(run(args))
    except KeyboardInterrupt:
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()

import json
from dataclasses import dataclass, field
from datetime import datetime

import redis.asyncio as aioredis

from .config import config


def _s(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


@dataclass
class TrackState:
    last_seen: datetime
    frame_seq: int
    frame_reference: str
    zones: set[str] = field(default_factory=set)
    entry_times: dict[str, datetime] = field(default_factory=dict)
    dwell_notified: set[str] = field(default_factory=set)

    def to_json(self) -> str:
        return json.dumps(
            {
                "last_seen": self.last_seen.isoformat(),
                "frame_seq": self.frame_seq,
                "frame_reference": self.frame_reference,
                "zones": sorted(self.zones),
                "entry_times": {
                    z: t.isoformat()
                    for z, t in self.entry_times.items()
                },
                "dwell_notified": sorted(self.dwell_notified),
            }
        )

    @classmethod
    def from_json(cls, raw) -> "TrackState":
        d = json.loads(_s(raw))
        return cls(
            last_seen=datetime.fromisoformat(d["last_seen"]),
            frame_seq=d["frame_seq"],
            frame_reference=d["frame_reference"],
            zones=set(d["zones"]),
            entry_times={
                z: datetime.fromisoformat(t)
                for z, t in d["entry_times"].items()
            },
            dwell_notified=set(d["dwell_notified"]),
        )


class ActiveTrackStore:
    """
    Per-camera active track state in the Redis hash zone:active:{camera_id}
    (field track_id -> TrackState JSON). One HGETALL to read, one pipeline
    to write. Redis errors propagate so the message is retried.
    """

    def __init__(self, redis_client: aioredis.Redis):
        self._redis = redis_client

    @staticmethod
    def _active_key(camera_id: str) -> str:
        return f"{config.ACTIVE_TRACKS_PREFIX}:{camera_id}"

    @staticmethod
    def _meta_key(camera_id: str) -> str:
        return f"{config.CAMERA_META_PREFIX}:{camera_id}"

    async def load(self, camera_id: str) -> dict[int, TrackState]:
        raw = await self._redis.hgetall(self._active_key(camera_id))

        return {
            int(_s(field_)): TrackState.from_json(value)
            for field_, value in raw.items()
        }

    async def save(
        self,
        camera_id: str,
        upserts: dict[int, TrackState],
        removals: list[int],
        *,
        wall_ts: float | None = None,
    ) -> None:
        """
        Apply changes in one pipeline. `wall_ts` (wall clock) refreshes the
        camera's liveness marker for the sweeper; None leaves it untouched.
        """

        key = self._active_key(camera_id)

        async with self._redis.pipeline(transaction=True) as pipe:
            if upserts:
                pipe.hset(
                    key,
                    mapping={
                        str(tid): st.to_json()
                        for tid, st in upserts.items()
                    },
                )
                pipe.expire(key, config.ACTIVE_STATE_TTL)
            if removals:
                pipe.hdel(key, *[str(t) for t in removals])
            if wall_ts is not None:
                pipe.set(
                    self._meta_key(camera_id),
                    json.dumps({"wall_ts": wall_ts}),
                    ex=config.ACTIVE_STATE_TTL,
                )
            await pipe.execute()

    async def clear_meta(self, camera_id: str) -> None:
        await self._redis.delete(self._meta_key(camera_id))

    async def stale_cameras(self, wall_now: float, stale_after_s: float) -> list[str]:
        prefix = f"{config.CAMERA_META_PREFIX}:"
        stale = []

        async for key in self._redis.scan_iter(match=f"{prefix}*"):
            key = _s(key)
            raw = await self._redis.get(key)
            if raw is None:
                continue
            if wall_now - json.loads(_s(raw))["wall_ts"] >= stale_after_s:
                stale.append(key[len(prefix):])

        return stale

"""Globally unique track ids.

ByteTrack ids are local to one tracker instance and restart at 1 whenever the
tracker (or the process) restarts. Downstream services key state on track id,
so they only ever see ids from a Redis counter: (camera_id, local_id) is
mapped to `INCRBY detection:track_seq`, and a restart can never reuse an id.
"""

import logging

logger = logging.getLogger(__name__)

TRACK_SEQ_KEY = "detection:track_seq"


class TrackIdAllocator:
    def __init__(self, redis, track_buffer: int, key: str = TRACK_SEQ_KEY) -> None:
        self._redis = redis
        self._track_buffer = track_buffer
        self._key = key
        # camera -> {local_id: [global_id, last_seen_update_no]}
        self._map: dict[str, dict[int, list[int]]] = {}
        self._updates: dict[str, int] = {}

    async def assign(self, camera_id: str, local_ids: list[int]) -> dict[int, int]:
        """Map this frame's local ids to global ids.

        Call once per tracker update, also for empty frames, so entries age
        in tracker frames. Entries unseen for `track_buffer` updates are
        dropped: ByteTrack has forgotten those tracks by then.
        """
        update_no = self._updates.get(camera_id, 0) + 1
        entries = self._map.setdefault(camera_id, {})

        new_ids = [i for i in dict.fromkeys(local_ids) if i not in entries]
        if new_ids:
            # One round trip for all new tracks of the frame. If it raises,
            # nothing has been recorded yet, so a retry is clean.
            last = int(await self._redis.incrby(self._key, len(new_ids)))
            first = last - len(new_ids) + 1
        self._updates[camera_id] = update_no

        for offset, local_id in enumerate(new_ids):
            entries[local_id] = [first + offset, update_no]
        for local_id in local_ids:
            entries[local_id][1] = update_no

        cutoff = update_no - self._track_buffer
        for local_id in [i for i, (_, seen) in entries.items() if seen < cutoff]:
            del entries[local_id]

        return {i: entries[i][0] for i in local_ids}

    def drop_camera(self, camera_id: str) -> None:
        """Forget a camera (tracker reset, camera removed)."""
        self._map.pop(camera_id, None)
        self._updates.pop(camera_id, None)

    def size(self, camera_id: str) -> int:
        return len(self._map.get(camera_id, {}))

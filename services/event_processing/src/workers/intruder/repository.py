"""All intruder SQL. Errors propagate (transient -> the message is retried)."""
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

_OPEN_COLUMNS = """
    id, classification_reason, person_id, alert_published, alert_id
"""


def _open_row(row) -> dict:
    return {
        "id": str(row.id),
        "classification_reason": row.classification_reason,
        "person_id": str(row.person_id) if row.person_id else None,
        "alert_published": row.alert_published,
        "alert_id": str(row.alert_id) if row.alert_id else None,
    }


_INSERT_OPEN = text(
    f"""
    INSERT INTO intruder_events (
        id, zone_id, camera_id, track_id, person_id,
        classification_reason, first_detected_at,
        last_seen_at, alert_status
    )
    VALUES (
        CAST(:id AS uuid), CAST(:zone_id AS uuid),
        CAST(:camera_id AS uuid), :track_id,
        CAST(:person_id AS uuid), :reason, :ts, :ts,
        'pending'
    )
    ON CONFLICT (camera_id, zone_id, track_id)
        WHERE alert_status <> 'resolved'
    DO NOTHING
    RETURNING {_OPEN_COLUMNS}
    """
)


class IntruderRepository:

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def fetch_zone(self, zone_id: str) -> dict | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, name, type FROM zones "
                        "WHERE id = CAST(:zone_id AS uuid)"
                    ),
                    {"zone_id": zone_id},
                )
            ).fetchone()

        if row is None:
            return None

        return {"id": str(row.id), "name": row.name, "type": row.type}

    async def authorized_person_ids(self, zone_id: str) -> list[str]:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT person_id FROM zone_authorized_persons "
                    "WHERE zone_id = CAST(:zone_id AS uuid)"
                ),
                {"zone_id": zone_id},
            )
            return [str(r.person_id) for r in result.fetchall()]

    async def recognitions_in_window(
        self,
        camera_id: str,
        track_id: int,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """Bounded lookup: (camera, track) within [start, end], best first."""

        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT id, person_id, identity_tag, similarity_score,
                           timestamp
                    FROM recognition_events
                    WHERE camera_id = CAST(:camera_id AS uuid)
                      AND track_id = :track_id
                      AND timestamp >= :start
                      AND timestamp <= :end
                    ORDER BY similarity_score DESC, timestamp DESC
                    """
                ),
                {
                    "camera_id": camera_id,
                    "track_id": track_id,
                    "start": start,
                    "end": end,
                },
            )
            rows = result.fetchall()

        return [
            {
                "id": str(r.id),
                "person_id": str(r.person_id) if r.person_id else None,
                "identity_tag": str(getattr(r.identity_tag, "value", r.identity_tag)),
                "similarity_score": float(r.similarity_score),
                "timestamp": r.timestamp,
            }
            for r in rows
        ]

    async def persons_on_other_tracks(
        self,
        camera_id: str,
        track_id: int,
        person_ids: list[str],
        start: datetime,
        end: datetime,
    ) -> set[str]:
        """Which of `person_ids` are ENROLLED matches on a DIFFERENT track of
        the same camera within [start, end] (bounded: one camera, one window)."""

        if not person_ids:
            return set()

        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT DISTINCT person_id
                    FROM recognition_events
                    WHERE camera_id = CAST(:camera_id AS uuid)
                      AND track_id <> :track_id
                      AND identity_tag = 'enrolled'
                      AND person_id = ANY(CAST(:ids AS uuid[]))
                      AND timestamp >= :start
                      AND timestamp <= :end
                    """
                ),
                {
                    "camera_id": camera_id,
                    "track_id": track_id,
                    "ids": person_ids,
                    "start": start,
                    "end": end,
                },
            )
            return {str(r.person_id) for r in result.fetchall()}

    async def blocklisted_person_ids(
        self, person_ids: list[str], at: datetime
    ) -> set[str]:
        """Which of `person_ids` are blocklisted at event time `at`."""

        if not person_ids:
            return set()

        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    SELECT DISTINCT person_id
                    FROM blocklist
                    WHERE person_id = ANY(CAST(:ids AS uuid[]))
                      AND (expires_at IS NULL OR expires_at > :at)
                    """
                ),
                {"ids": person_ids, "at": at},
            )
            return {str(r.person_id) for r in result.fetchall()}

    async def get_open(
        self, camera_id: str, zone_id: str, track_id: int
    ) -> dict | None:
        async with self._session_factory() as session:
            row = await self._select_open(
                session, camera_id, zone_id, track_id
            )
        return _open_row(row) if row else None

    @staticmethod
    async def _select_open(session, camera_id, zone_id, track_id):
        return (
            await session.execute(
                text(
                    f"""
                    SELECT {_OPEN_COLUMNS}
                    FROM intruder_events
                    WHERE camera_id = CAST(:camera_id AS uuid)
                      AND zone_id = CAST(:zone_id AS uuid)
                      AND track_id = :track_id
                      AND alert_status <> 'resolved'
                    """
                ),
                {
                    "camera_id": camera_id,
                    "zone_id": zone_id,
                    "track_id": track_id,
                },
            )
        ).fetchone()

    async def create_or_get_open(
        self,
        event_id: UUID,
        camera_id: str,
        zone_id: str,
        track_id: int,
        person_id: str | None,
        reason: str,
        timestamp: datetime,
    ) -> dict | None:
        """
        Idempotent create. Returns the open row (new or pre-existing), so
        the caller can publish it if `alert_published` is still false.

        Returns None when `event_id` already exists but is resolved: that
        zone event was fully handled earlier (a late redelivery), so there
        is nothing left to alert.
        """

        params = {
            "id": str(event_id),
            "camera_id": camera_id,
            "zone_id": zone_id,
            "track_id": track_id,
            "person_id": person_id,
            "reason": reason,
            "ts": timestamp,
        }

        try:
            async with self._session_factory() as session, session.begin():
                inserted = (
                    await session.execute(_INSERT_OPEN, params)
                ).fetchone()

                if inserted is not None:
                    return _open_row(inserted)

                existing = await self._select_open(
                    session, camera_id, zone_id, track_id
                )
        except IntegrityError as exc:
            # ON CONFLICT above only arbitrates the open-track index. The
            # same zone event redelivered (concurrently, or after its row
            # was resolved) collides on the primary key instead.
            if getattr(exc.orig, "sqlstate", None) != "23505":
                raise

            async with self._session_factory() as session:
                by_id = (
                    await session.execute(
                        text(
                            f"""
                            SELECT {_OPEN_COLUMNS}, alert_status
                            FROM intruder_events
                            WHERE id = CAST(:id AS uuid)
                            """
                        ),
                        {"id": str(event_id)},
                    )
                ).fetchone()

            if by_id is None or by_id.alert_status == "resolved":
                return None

            return _open_row(by_id)

        if existing is None:
            # Resolved between the INSERT and the SELECT: let the retry decide.
            raise RuntimeError(
                "open intruder event vanished during create_or_get_open"
            )

        return _open_row(existing)

    async def touch(self, event_id: str, timestamp: datetime) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE intruder_events "
                    "SET last_seen_at = GREATEST(last_seen_at, :ts) "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": event_id, "ts": timestamp},
            )

    async def mark_published(self, event_id: str, alert_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE intruder_events "
                    "SET alert_published = true, "
                    "    alert_id = CAST(:alert_id AS uuid) "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {"id": event_id, "alert_id": str(alert_id)},
            )

    async def resolve_open(
        self,
        camera_id: str,
        zone_id: str,
        track_id: int,
        timestamp: datetime,
    ) -> bool:
        """Resolve the open event at event time. False if none was open."""

        async with self._session_factory() as session, session.begin():
            row = (
                await session.execute(
                    text(
                        """
                        UPDATE intruder_events
                        SET alert_status = 'resolved',
                            resolved_at = :ts,
                            last_seen_at = GREATEST(last_seen_at, :ts)
                        WHERE camera_id = CAST(:camera_id AS uuid)
                          AND zone_id = CAST(:zone_id AS uuid)
                          AND track_id = :track_id
                          AND alert_status <> 'resolved'
                        RETURNING id
                        """
                    ),
                    {
                        "camera_id": camera_id,
                        "zone_id": zone_id,
                        "track_id": track_id,
                        "ts": timestamp,
                    },
                )
            ).fetchone()

        return row is not None

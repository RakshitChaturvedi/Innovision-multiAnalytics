"""
In-memory cache of all enrolled persons' embeddings.

Startup: loads all enrollment embeddings from PostgreSQL into a numpy
matrix. Updates: Redis Pub/Sub invalidation triggers a partial reload
per person. This is the primary recognition lookup — zero DB hits on
the hot path.
"""
import asyncio
import logging
from typing import Any

import numpy as np
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import config
from .supervised import supervise

logger = logging.getLogger(__name__)


def parse_vector(value: Any) -> np.ndarray | None:
    """Turn what asyncpg hands back for a pgvector value into float32.

    Without pgvector's codec the value arrives as text ('[0.1,0.2,...]'); with
    it, as an ndarray/list. All are accepted so the cache does not depend on
    connection-level codec registration.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        body = value.strip().strip("[]")
        if not body:
            return None
        return np.array(body.split(","), dtype=np.float32)
    return np.asarray(value, dtype=np.float32)


def _mean_unit(raw_embeddings) -> np.ndarray | None:
    vecs = [v for v in (parse_vector(e) for e in raw_embeddings if e is not None) if v is not None]
    if not vecs:
        return None
    avg = np.mean(vecs, axis=0).astype(np.float32)
    norm = np.linalg.norm(avg)
    if norm == 0:
        return None
    return (avg / norm).astype(np.float32)


class EmbeddingCache:
    """
    enrolled_matrix:    np.ndarray (N, 512) — one row per enrolled person
    enrolled_persons:   list[dict] — same index as matrix rows
    person_id_to_idx:   dict[str, int] — fast partial updates
    """

    def __init__(self) -> None:
        self.enrolled_matrix: np.ndarray = np.zeros((0, 512), dtype=np.float32)
        self.enrolled_persons: list[dict] = []
        self.person_id_to_idx: dict[str, int] = {}

        self._engine = create_async_engine(config.DATABASE_URL)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._redis: aioredis.Redis | None = None
        self._listener_task: asyncio.Task | None = None
        self._listener_runs = 0

    async def initialize(self, redis_client: aioredis.Redis) -> None:
        self._redis = redis_client
        await self._load_all()
        self._listener_task = asyncio.create_task(
            supervise("embedding_cache_listener", self._listen_invalidations),
            name="embedding-cache-listener",
        )
        logger.info("embedding_cache_ready enrolled_count=%d", len(self.enrolled_persons))

    async def close(self) -> None:
        task, self._listener_task = self._listener_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._engine.dispose()

    async def _load_all(self) -> None:
        async with self._session_factory() as session:
            rows = await session.execute(text("""
                SELECT
                    ep.id AS person_id, ep.name, ep.role, ep.department, ep.clearance_level,
                    array_agg(fe.embedding::text ORDER BY fe.created_at) AS embeddings
                FROM enrolled_persons ep
                JOIN face_embeddings fe ON fe.person_id = ep.id AND fe.is_enrollment = true
                GROUP BY ep.id, ep.name, ep.role, ep.department, ep.clearance_level
            """))
            all_rows = rows.fetchall()

        persons, matrix_rows = [], []
        for row in all_rows:
            avg = _mean_unit(row.embeddings)
            if avg is None:
                continue
            persons.append({
                "person_id": str(row.person_id),
                "name": row.name,
                "role": row.role,
                "department": row.department,
                "clearance_level": row.clearance_level,
            })
            matrix_rows.append(avg)

        # Replace atomically, also when empty: persons removed while we were
        # not listening must not linger in the matrix.
        matrix = np.stack(matrix_rows) if matrix_rows else np.zeros((0, 512), dtype=np.float32)
        self.enrolled_matrix = matrix
        self.enrolled_persons = persons
        self.person_id_to_idx = {p["person_id"]: i for i, p in enumerate(persons)}
        logger.info("embedding_cache_loaded count=%d shape=%s", len(persons), matrix.shape)

    async def reload_person(self, person_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.execute(text("""
                SELECT
                    ep.id, ep.name, ep.role, ep.department, ep.clearance_level,
                    array_agg(fe.embedding::text ORDER BY fe.created_at) AS embeddings
                FROM enrolled_persons ep
                JOIN face_embeddings fe ON fe.person_id = ep.id AND fe.is_enrollment = true
                WHERE ep.id = :person_id
                GROUP BY ep.id, ep.name, ep.role, ep.department, ep.clearance_level
            """), {"person_id": person_id})
            result = row.fetchone()

        if result is None:
            await self._remove_person(person_id)
            return

        avg = _mean_unit(result.embeddings)
        if avg is None:
            await self._remove_person(person_id)
            return

        new_person = {
            "person_id": str(result.id),
            "name": result.name,
            "role": result.role,
            "department": result.department,
            "clearance_level": result.clearance_level,
        }

        if person_id in self.person_id_to_idx:
            idx = self.person_id_to_idx[person_id]
            self.enrolled_matrix[idx] = avg
            self.enrolled_persons[idx] = new_person
        else:
            self.enrolled_matrix = np.vstack([self.enrolled_matrix, avg.reshape(1, -1)])
            new_idx = len(self.enrolled_persons)
            self.enrolled_persons.append(new_person)
            self.person_id_to_idx[person_id] = new_idx

        logger.info("embedding_cache_updated person_id=%s", person_id)

    async def _remove_person(self, person_id: str) -> None:
        if person_id not in self.person_id_to_idx:
            return
        idx = self.person_id_to_idx.pop(person_id)
        self.enrolled_persons.pop(idx)
        self.enrolled_matrix = np.delete(self.enrolled_matrix, idx, axis=0)
        self.person_id_to_idx = {p["person_id"]: i for i, p in enumerate(self.enrolled_persons)}
        logger.info("embedding_cache_removed person_id=%s", person_id)

    async def _listen_invalidations(self) -> None:
        """One subscription session; the supervisor restarts it on any failure."""
        pubsub = self._redis.pubsub()
        try:
            await pubsub.subscribe(config.ENROLL_INVALIDATE_CHANNEL)
            logger.info("embedding_cache_listening channel=%s", config.ENROLL_INVALIDATE_CHANNEL)
            self._listener_runs += 1
            if self._listener_runs > 1:
                # Invalidations published while we were disconnected are lost.
                await self._load_all()

            async for message in pubsub.listen():
                if message["type"] == "message":
                    person_id = message["data"]
                    if isinstance(person_id, bytes):
                        person_id = person_id.decode()
                    logger.info("embedding_cache_invalidate person_id=%s", person_id)
                    await self.reload_person(person_id)
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                logger.warning("embedding_cache_pubsub_close_failed", exc_info=True)

    def search(self, query_embedding: np.ndarray, threshold: float) -> tuple[dict | None, float]:
        """
        Cosine similarity search against the enrolled matrix. Pure
        in-process computation, no I/O. Both the query and the enrolled
        rows are unit-normalized, so dot product == cosine similarity.
        """
        if len(self.enrolled_matrix) == 0:
            return None, 0.0

        similarities = self.enrolled_matrix @ query_embedding
        best_idx = int(np.argmax(similarities))
        best_score = float(similarities[best_idx])

        if best_score >= threshold:
            return self.enrolled_persons[best_idx], best_score

        return None, best_score

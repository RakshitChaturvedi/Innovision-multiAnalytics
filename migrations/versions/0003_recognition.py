"""recognition: enrolled_persons, face_embeddings, recognition_events,
persons_seen, camera_config

Revision ID: 0003
Revises: 0002

WHY THIS MIGRATION EXISTS
--------------------------
None of the tables the recognition service reads or writes exist yet:
CameraConfigStore.get() queries camera_config, EmbeddingCache queries
enrolled_persons/face_embeddings, and RecognitionConsumer._persist_and_publish
writes face_embeddings + recognition_events + enrolled_persons.last_seen_at
in one transaction. Every one of those currently fails with
UndefinedTableError.

Column set matches the actual queries in services/recognition/src/
(consumer.py, embedding_cache.py, camera_config_store.py) exactly, not the
original sprint-plan draft, which had drifted from what the code does.

Design notes
------------
- No `cameras` table exists anywhere in migrations yet (0002 only created
  detection_events; there's no 0002_cameras as the original plan assumed).
  face_embeddings.source_camera_id, recognition_events.camera_id, and
  camera_config.camera_id are therefore plain UUID columns with NO foreign
  key for now. Add the FK once a cameras migration exists — don't invent
  one here, that table isn't owned by the recognition service.
- embedding is `vector(512)` (pgvector, unit-normalized ArcFace output).
  HNSW index included for the DB-fallback search path described in the
  sprint plan; the hot-path search is in-process in EmbeddingCache and
  does not use this index, but it's cheap to have ready for the API's
  enrollment-dedup lookups later.
- identity_tag reuses the `identity_tag` enum type already created in
  0001_base.py — do not redefine it here.
- persons_seen matches the original spec's cross-camera identity-tracking
  table. No code in services/recognition currently writes to it — it's
  included for schema completeness since it was part of the documented
  0003 design, not because anything depends on it today.
- camera_config has camera_id as its own primary key (one row per camera,
  not a surrogate id) since CameraConfigStore.get() always looks it up by
  camera_id directly.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from pgvector.sqlalchemy import Vector


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:

    # ------------------------------------------------------------------
    # enrolled_persons
    # ------------------------------------------------------------------

    op.create_table(
        "enrolled_persons",
        sa.Column(
            "id", UUID, primary_key=True, nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(100), nullable=True),
        sa.Column("department", sa.String(100), nullable=True),
        sa.Column("clearance_level", sa.Integer, nullable=False, server_default="1"),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "enrolled_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # ------------------------------------------------------------------
    # face_embeddings
    # ------------------------------------------------------------------

    op.create_table(
        "face_embeddings",
        sa.Column(
            "id", UUID, primary_key=True, nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "person_id", UUID,
            sa.ForeignKey("enrolled_persons.id", ondelete="CASCADE"),
            nullable=True,  # nullable: recognition writes a row even on UNKNOWN matches
        ),
        sa.Column("embedding", Vector(512), nullable=False),
        # No FK — no cameras table exists yet. See module docstring.
        sa.Column("source_camera_id", UUID, nullable=True),
        sa.Column("quality_score", sa.Float, nullable=False),
        sa.Column(
            "is_enrollment", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "quality_score >= 0 AND quality_score <= 1",
            name="ck_face_embeddings_quality_range",
        ),
    )

    # EmbeddingCache._load_all() / reload_person() filter on
    # (person_id, is_enrollment) and order by created_at.
    op.create_index(
        "ix_face_embeddings_person_enrollment",
        "face_embeddings",
        ["person_id", "is_enrollment", "created_at"],
    )

    # DB-fallback cosine similarity search (not the hot path — that's
    # in-process in EmbeddingCache — but needed for enrollment dedup /
    # admin lookups per the sprint plan).
    op.execute("""
        CREATE INDEX ix_face_embeddings_vector_cosine
        ON face_embeddings
        USING hnsw (embedding vector_cosine_ops)
    """)

    # ------------------------------------------------------------------
    # recognition_events
    # ------------------------------------------------------------------

    op.create_table(
        "recognition_events",
        sa.Column(
            "id", UUID, primary_key=True, nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # No FK — no cameras table exists yet. See module docstring.
        sa.Column("camera_id", UUID, nullable=False),
        sa.Column("detection_event_id", UUID, nullable=False),
        sa.Column("track_id", sa.Integer, nullable=False),
        sa.Column(
            "person_id", UUID,
            sa.ForeignKey("enrolled_persons.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("similarity_score", sa.Float, nullable=False),
        # Reuses the identity_tag enum type created in 0001_base.py.
        sa.Column(
            "identity_tag",
            sa.Enum("enrolled", "visitor", "unknown", name="identity_tag", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "embedding_id", UUID,
            sa.ForeignKey("face_embeddings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quality_score", sa.Float, nullable=False),
        sa.Column("liveness_score", sa.Float, nullable=True),
        sa.Column(
            "liveness_checked", sa.Boolean, nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "similarity_score >= 0 AND similarity_score <= 1",
            name="ck_recognition_events_similarity_range",
        ),
        sa.CheckConstraint(
            "quality_score >= 0 AND quality_score <= 1",
            name="ck_recognition_events_quality_range",
        ),
    )

    # Dashboard-style "recent activity on camera X" queries — same
    # pattern as ix_detection_events_camera_time in 0002.
    op.create_index(
        "ix_recognition_events_camera_time",
        "recognition_events",
        ["camera_id", "timestamp"],
    )

    # "Everywhere this person was seen" lookups.
    op.create_index(
        "ix_recognition_events_person_id",
        "recognition_events",
        ["person_id"],
    )

    # ------------------------------------------------------------------
    # persons_seen — cross-camera identity tracking.
    # NOT written by any current recognition service code path; included
    # for schema completeness per the original 0003 design. See module
    # docstring.
    # ------------------------------------------------------------------

    op.create_table(
        "persons_seen",
        sa.Column(
            "id", UUID, primary_key=True, nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "person_id", UUID,
            sa.ForeignKey("enrolled_persons.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "global_id", UUID, nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("track_id", sa.Integer, nullable=False),
        # No FK — no cameras table exists yet. See module docstring.
        sa.Column("first_camera_id", UUID, nullable=False),
        sa.Column("last_camera_id", UUID, nullable=False),
        sa.Column("first_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )

    # ------------------------------------------------------------------
    # camera_config — read by CameraConfigStore.get(). One row per
    # camera; camera_id is the primary key itself, not a surrogate id.
    # ------------------------------------------------------------------

    op.create_table(
        "camera_config",
        # No FK — no cameras table exists yet. See module docstring.
        sa.Column("camera_id", UUID, primary_key=True, nullable=False),
        sa.Column("similarity_threshold", sa.Float, nullable=False, server_default="0.60"),
        sa.Column("min_face_size_px", sa.Integer, nullable=False, server_default="40"),
        sa.Column("blur_threshold", sa.Float, nullable=False, server_default="100.0"),
        sa.Column("recognition_sample_rate", sa.Integer, nullable=False, server_default="10"),
        sa.CheckConstraint(
            "similarity_threshold >= 0 AND similarity_threshold <= 1",
            name="ck_camera_config_similarity_range",
        ),
    )


def downgrade() -> None:
    op.drop_table("camera_config")
    op.drop_table("persons_seen")
    op.drop_index("ix_recognition_events_person_id", table_name="recognition_events")
    op.drop_index("ix_recognition_events_camera_time", table_name="recognition_events")
    op.drop_table("recognition_events")
    op.execute("DROP INDEX IF EXISTS ix_face_embeddings_vector_cosine")
    op.drop_index("ix_face_embeddings_person_enrollment", table_name="face_embeddings")
    op.drop_table("face_embeddings")
    op.drop_table("enrolled_persons")

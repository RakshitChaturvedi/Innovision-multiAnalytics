"""base: extensions, domain enums, and audit_log

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000

What this migration does and why
---------------------------------
1.  Extensions
    - pgvector  : required by the recognition service for face-embedding
                  similarity search (vector columns appear in future migrations).
    - uuid-ossp : provides gen_random_uuid() used as DEFAULT on all UUID PKs
                  across every domain table.

2.  PostgreSQL ENUM types for every enum in shared/schemas/enums.py
    Created here — in the base migration — so that every subsequent
    migration (cameras, persons, zones, detections …) can reference them
    without re-declaring them.  Two enums (crowd_model, event_type) are
    imported by events.py but not yet defined in enums.py; they are
    stubbed here so future migrations don't break.

3.  audit_log table
    Schema is derived directly from shared/audit/writer.py AuditWriter.log():

        INSERT INTO audit_log
            (service, action, entity_type, entity_id,
             operator_id, metadata, timestamp)
        VALUES (...)

    Column-level decisions:
    - id          : BIGSERIAL — not supplied by writer, auto-generated.
    - entity_id   : TEXT — writer does str(entity_id) before inserting,
                    even though the Python param is typed int.  TEXT is the
                    safe choice and matches the actual wire value.
    - operator_id : TEXT — writer does str(operator_id) before inserting.
    - metadata    : JSONB — writer passes a dict; cast to ::jsonb in insert.
    - timestamp   : TIMESTAMPTZ with server default now() — writer supplies
                    an explicit value but the default guards against omission.

4.  Append-only enforcement
    PostgreSQL RULE (not trigger) chosen deliberately:
    - Rules fire at the rewrite level before triggers, making them harder
      to accidentally bypass from application code.
    - DO INSTEAD NOTHING silently discards UPDATE/DELETE rather than raising
      an error, which prevents the audit writer itself from crashing if it
      ever calls session.commit() after an accidental ORM-level mutation.
    - A future DBA-only escape hatch: DROP RULE, perform maintenance, re-add.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# ---------------------------------------------------------------------------
# Revision identifiers
# ---------------------------------------------------------------------------

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_enum(name: str, *values: str) -> None:
    """Create a PostgreSQL ENUM type if it does not already exist."""
    quoted = ", ".join(f"'{v}'" for v in values)
    op.execute(f"CREATE TYPE {name} AS ENUM ({quoted})")


def _drop_enum(name: str) -> None:
    op.execute(f"DROP TYPE IF EXISTS {name}")


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:

    # ------------------------------------------------------------------
    # 1. Extensions
    # ------------------------------------------------------------------

    # pgvector — face/body embedding similarity search (recognition service)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # uuid-ossp — gen_random_uuid() for UUID primary key defaults
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # ------------------------------------------------------------------
    # 2. PostgreSQL ENUM types (derived from shared/schemas/enums.py)
    #    Prefixed with "innovision_" to avoid name collisions with any
    #    Postgres built-ins or future extensions.
    # ------------------------------------------------------------------

    # CameraProfile
    _create_enum(
        "camera_profile",
        "high_security", "balanced", "high_throughput", "crowd_only",
    )

    # IdentityTag
    _create_enum(
        "identity_tag",
        "enrolled", "visitor", "unknown",
    )

    # AlertType
    _create_enum(
        "alert_type",
        "restricted_entry", "intruder", "headcount_breach",
        "crowd_density", "pedestrian_anomaly",
    )

    # AlertSeverity
    _create_enum(
        "alert_severity",
        "critical", "high", "medium", "low",
    )

    # AlertStatus
    _create_enum(
        "alert_status",
        "pending", "acknowledged", "resolved",
    )

    # ZoneType
    _create_enum(
        "zone_type",
        "restricted", "monitored", "safe",
    )

    # ZoneEventType
    _create_enum(
        "zone_event_type",
        "entered", "exited", "dwell",
    )

    # DensityLevel
    _create_enum(
        "density_level",
        "low", "medium", "high", "critical",
    )

    # CameraStatus
    _create_enum(
        "camera_status",
        "online", "offline", "reconnecting",
    )

    # FeedbackType
    _create_enum(
        "feedback_type",
        "confirm", "reject",
    )

    # DataCategory
    _create_enum(
        "data_category",
        "visitor_embeddings", "recognition_events", "detection_events",
        "snapshots", "audit_log",
    )

    # Stubs for enums imported by events.py but not yet in enums.py.
    # Values are placeholders — update these when the enums are defined.
    _create_enum("crowd_model", "csp", "mcnn", "bayesian")    # TODO: confirm values
    _create_enum("event_type", "frame", "detection", "recognition", "alert")  # TODO: confirm values

    # ------------------------------------------------------------------
    # 3. audit_log table
    #    Schema mirrors the INSERT in shared/audit/writer.py exactly.
    # ------------------------------------------------------------------

    op.create_table(
        "audit_log",
        # Auto-generated surrogate key — not supplied by AuditWriter.log()
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
            comment="Surrogate key, auto-generated, never supplied by application",
        ),
        # service — which microservice emitted this entry
        # e.g. "ingestion", "detection", "recognition", "event_processing", "api"
        sa.Column(
            "service",
            sa.Text(),
            nullable=False,
            comment="Microservice name: ingestion | detection | recognition | event_processing | api",
        ),
        # action — what happened, e.g. "frame.ingested", "alert.raised", "person.enrolled"
        sa.Column(
            "action",
            sa.Text(),
            nullable=False,
            comment="Domain action, dot-namespaced: <entity>.<verb>",
        ),
        # entity_type — which domain object was affected
        # Aligns with DataCategory enum values where applicable
        sa.Column(
            "entity_type",
            sa.Text(),
            nullable=False,
            comment="Domain entity type, e.g. camera, person, alert, zone",
        ),
        # entity_id — TEXT because writer.py does str(entity_id) before inserting
        # even when the source value is a UUID or integer
        sa.Column(
            "entity_id",
            sa.Text(),
            nullable=True,
            comment="String-cast primary key of the affected entity (UUID or int cast to TEXT)",
        ),
        # operator_id — UUID of the human/service that triggered the action;
        # NULL for fully automated pipeline events
        sa.Column(
            "operator_id",
            sa.Text(),
            nullable=True,
            comment="String-cast UUID of the operator; NULL for automated pipeline events",
        ),
        # metadata — arbitrary extra context (JSONB for indexing support)
        # writer.py passes a plain dict; psycopg2/asyncpg serialises it
        sa.Column(
            "metadata",
            JSONB(),
            nullable=True,
            server_default=sa.text("'{}'::jsonb"),
            comment="Arbitrary key-value context; JSONB for GIN-indexable queries",
        ),
        # timestamp — always UTC; writer supplies an explicit value,
        # server default guards against accidental omission
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="UTC timestamp of the event; supplied by writer, server default is a safety net",
        ),
        # Table-level comment
        comment=(
            "Append-only audit trail for all Innovision services. "
            "UPDATE and DELETE are blocked at the DB level via PostgreSQL rules."
        ),
    )

    # Index: time-range queries are the primary access pattern on audit_log
    op.create_index(
        "ix_audit_log_timestamp",
        "audit_log",
        ["timestamp"],
    )

    # Index: per-service filtering (dashboard, alerting)
    op.create_index(
        "ix_audit_log_service_action",
        "audit_log",
        ["service", "action"],
    )

    # Index: per-entity history lookup
    op.create_index(
        "ix_audit_log_entity",
        "audit_log",
        ["entity_type", "entity_id"],
    )

    # ------------------------------------------------------------------
    # 4. Append-only enforcement via PostgreSQL RULE
    #
    #    WHY RULE over TRIGGER:
    #    Rules rewrite the query before execution — they cannot be
    #    bypassed by SET session_replication_role = replica (unlike
    #    triggers).  DO INSTEAD NOTHING means the statement silently
    #    succeeds with 0 rows affected, preventing any exception from
    #    bubbling up through SQLAlchemy's async session.
    # ------------------------------------------------------------------

    op.execute("""
        CREATE RULE audit_log_no_update AS
            ON UPDATE TO audit_log
            DO INSTEAD NOTHING
    """)

    op.execute("""
        CREATE RULE audit_log_no_delete AS
            ON DELETE TO audit_log
            DO INSTEAD NOTHING
    """)


# ---------------------------------------------------------------------------
# Downgrade — full teardown in reverse dependency order
# ---------------------------------------------------------------------------

def downgrade() -> None:

    # Remove append-only rules first
    op.execute("DROP RULE IF EXISTS audit_log_no_delete ON audit_log")
    op.execute("DROP RULE IF EXISTS audit_log_no_update ON audit_log")

    # Drop indexes (Alembic drops them with the table, but being explicit
    # is safer if the table constraint names ever drift)
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_index("ix_audit_log_service_action", table_name="audit_log")
    op.drop_index("ix_audit_log_timestamp", table_name="audit_log")

    # Drop table
    op.drop_table("audit_log")

    # Drop stub enums
    _drop_enum("event_type")
    _drop_enum("crowd_model")

    # Drop domain enums (reverse order of creation)
    _drop_enum("data_category")
    _drop_enum("feedback_type")
    _drop_enum("camera_status")
    _drop_enum("density_level")
    _drop_enum("zone_event_type")
    _drop_enum("zone_type")
    _drop_enum("alert_status")
    _drop_enum("alert_severity")
    _drop_enum("alert_type")
    _drop_enum("identity_tag")
    _drop_enum("camera_profile")

    # Drop extensions last (other objects may depend on them)
    op.execute('DROP EXTENSION IF EXISTS "uuid-ossp"')
    op.execute("DROP EXTENSION IF EXISTS vector")

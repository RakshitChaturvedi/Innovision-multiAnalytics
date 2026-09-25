"""add zone monitoring tables"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0003_zone_monitor"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:

    op.create_table(
        "zones",

        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text(
                "gen_random_uuid()"
            ),
        ),

        sa.Column(
            "camera_id",
            UUID,
            nullable=False,
        ),

        sa.Column(
            "name",
            sa.String(255),
            nullable=False,
        ),

        sa.Column(
            "type",
            sa.String(32),
            nullable=False,
        ),

        sa.Column(
            "polygon",
            JSONB,
            nullable=False,
        ),

        sa.Column(
            "max_headcount",
            sa.Integer,
            nullable=True,
        ),

        sa.Column(
            "dwell_threshold_seconds",
            sa.Integer,
            nullable=False,
            server_default="60",
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),

        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_zones_camera_id",
        "zones",
        ["camera_id"],
    )

    op.create_table(
        "zone_authorized_persons",

        sa.Column(
            "zone_id",
            UUID,
            nullable=False,
        ),

        sa.Column(
            "person_id",
            UUID,
            nullable=False,
        ),

        sa.PrimaryKeyConstraint(
            "zone_id",
            "person_id",
        ),

        sa.ForeignKeyConstraint(
            ["zone_id"],
            ["zones.id"],
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "zone_events",

        sa.Column(
            "id",
            UUID,
            primary_key=True,
        ),

        sa.Column(
            "camera_id",
            UUID,
            nullable=False,
        ),

        sa.Column(
            "zone_id",
            UUID,
            nullable=False,
        ),

        sa.Column(
            "frame_seq",
            sa.BigInteger,
            nullable=False,
        ),

        sa.Column(
            "track_id",
            sa.Integer,
            nullable=False,
        ),

        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "event_type",
            sa.String(32),
            nullable=False,
        ),

        sa.Column(
            "dwell_duration_seconds",
            sa.Float,
            nullable=True,
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_index(
        "ix_zone_events_camera_time",
        "zone_events",
        ["camera_id", "timestamp"],
    )

    op.create_index(
        "ix_zone_events_zone_time",
        "zone_events",
        ["zone_id", "timestamp"],
    )


def downgrade() -> None:

    op.drop_index(
        "ix_zone_events_zone_time",
        table_name="zone_events",
    )

    op.drop_index(
        "ix_zone_events_camera_time",
        table_name="zone_events",
    )

    op.drop_table("zone_events")

    op.drop_table(
        "zone_authorized_persons"
    )

    op.drop_index(
        "ix_zones_camera_id",
        table_name="zones",
    )

    op.drop_table("zones")
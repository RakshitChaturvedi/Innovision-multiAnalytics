"""add intruder detection tables"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0004_intruder"

down_revision = (
    "0003_recognition",
    "0003_zone_monitor",
)

branch_labels = None
depends_on = None


def upgrade() -> None:

    # ---------------------------------------------------------------
    # Blocklist
    # ---------------------------------------------------------------

    op.create_table(
        "blocklist",

        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text(
                "gen_random_uuid()"
            ),
        ),

        sa.Column(
            "person_id",
            UUID,
            nullable=True,
        ),

        sa.Column(
            "embedding_id",
            UUID,
            nullable=True,
        ),

        sa.Column(
            "reason",
            sa.Text,
            nullable=False,
        ),

        sa.Column(
            "added_by",
            UUID,
            nullable=True,
        ),

        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),

        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_blocklist_person_id",
        "blocklist",
        ["person_id"],
    )

    op.create_index(
        "ix_blocklist_expires_at",
        "blocklist",
        ["expires_at"],
    )

    # ---------------------------------------------------------------
    # Intruder events
    # ---------------------------------------------------------------

    op.create_table(
        "intruder_events",

        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text(
                "gen_random_uuid()"
            ),
        ),

        sa.Column(
            "zone_id",
            UUID,
            nullable=False,
        ),

        sa.Column(
            "camera_id",
            UUID,
            nullable=False,
        ),

        sa.Column(
            "track_id",
            sa.Integer,
            nullable=False,
        ),

        sa.Column(
            "person_id",
            UUID,
            nullable=True,
        ),

        sa.Column(
            "classification_reason",
            sa.String(64),
            nullable=False,
        ),

        sa.Column(
            "first_detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "alert_status",
            sa.Enum(
                "pending",
                "acknowledged",
                "resolved",
                name="alert_status",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),

        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_index(
        "ix_intruder_events_camera_zone_track",
        "intruder_events",
        [
            "camera_id",
            "zone_id",
            "track_id",
        ],
    )

    op.create_index(
        "ix_intruder_events_status",
        "intruder_events",
        ["alert_status"],
    )

    # Only one open intruder event is allowed for a
    # given camera + zone + track.
    op.execute(
        """
        CREATE UNIQUE INDEX
        ux_intruder_events_open_track_zone
        ON intruder_events (
            camera_id,
            zone_id,
            track_id
        )
        WHERE alert_status != 'resolved'
        """
    )


def downgrade() -> None:

    op.execute(
        """
        DROP INDEX IF EXISTS
        ux_intruder_events_open_track_zone
        """
    )

    op.drop_index(
        "ix_intruder_events_status",
        table_name="intruder_events",
    )

    op.drop_index(
        "ix_intruder_events_camera_zone_track",
        table_name="intruder_events",
    )

    op.drop_table(
        "intruder_events"
    )

    op.drop_index(
        "ix_blocklist_expires_at",
        table_name="blocklist",
    )

    op.drop_index(
        "ix_blocklist_person_id",
        table_name="blocklist",
    )

    op.drop_table(
        "blocklist"
    )
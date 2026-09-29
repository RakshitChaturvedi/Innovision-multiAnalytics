"""reliability: alert publish tracking, one open breach per zone, indexes

Revision ID: 0006_reliability
Revises: 0005_headcount
"""

from alembic import op

revision = "0006_reliability"
down_revision = "0005_headcount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE intruder_events
            ADD COLUMN alert_published BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN alert_id UUID NULL
        """
    )

    op.execute(
        """
        ALTER TABLE headcount_breach_events
            ADD COLUMN alert_published BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN alert_id UUID NULL,
            ADD COLUMN resolution_reason VARCHAR(32) NULL
        """
    )
    # Old code could leave several open breaches per zone, which would make
    # the unique index below fail. Keep the newest open one, close the rest.
    op.execute(
        """
        UPDATE headcount_breach_events b
        SET alert_status = 'resolved',
            resolved_at = COALESCE(b.resolved_at, b.timestamp),
            resolution_reason = 'duplicate_migrated'
        WHERE b.alert_status <> 'resolved'
          AND EXISTS (
              SELECT 1 FROM headcount_breach_events n
              WHERE n.zone_id = b.zone_id
                AND n.alert_status <> 'resolved'
                AND (n.timestamp, n.id) > (b.timestamp, b.id)
          )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_headcount_open_breach
        ON headcount_breach_events (zone_id)
        WHERE alert_status <> 'resolved'
        """
    )

    op.execute(
        """
        CREATE INDEX ix_recognition_events_cam_track_ts
        ON recognition_events (camera_id, track_id, timestamp DESC)
        """
    )

    op.execute("ALTER TABLE zone_events ADD COLUMN frame_reference TEXT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE zone_events DROP COLUMN frame_reference")
    op.execute("DROP INDEX ix_recognition_events_cam_track_ts")
    op.execute("DROP INDEX ux_headcount_open_breach")
    op.execute(
        """
        ALTER TABLE headcount_breach_events
            DROP COLUMN resolution_reason,
            DROP COLUMN alert_id,
            DROP COLUMN alert_published
        """
    )
    op.execute(
        """
        ALTER TABLE intruder_events
            DROP COLUMN alert_id,
            DROP COLUMN alert_published
        """
    )

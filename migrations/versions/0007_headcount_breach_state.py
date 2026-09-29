"""headcount breach state: status column, one open breach per zone

resolution_reason and alert_published/alert_id are added by 0006_reliability.
The code keys on status = 'open', so this replaces 0006's alert_status-based
ux_headcount_open_breach with uq_headcount_breach_open_zone.

Revision ID: 0007_headcount_breach_state
Revises: 0006_reliability
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_headcount_breach_state"
down_revision = "0006_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "headcount_breach_events",
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
    )
    op.execute(
        "UPDATE headcount_breach_events SET status = 'resolved' "
        "WHERE alert_status = 'resolved'"
    )
    # Rows written before this migration were assumed already alerted.
    op.execute(
        "UPDATE headcount_breach_events SET alert_published = true "
        "WHERE status = 'open'"
    )
    # Old code could leave several open rows per zone: keep the newest only.
    op.execute(
        """
        UPDATE headcount_breach_events b
        SET status = 'resolved', alert_status = 'resolved',
            resolved_at = COALESCE(b.resolved_at, now()),
            resolution_reason = 'superseded'
        WHERE b.status = 'open' AND EXISTS (
            SELECT 1 FROM headcount_breach_events n
            WHERE n.zone_id = b.zone_id AND n.status = 'open'
              AND (n.timestamp, n.id) > (b.timestamp, b.id)
        )
        """
    )
    op.execute("DROP INDEX IF EXISTS ux_headcount_open_breach")
    op.execute(
        "CREATE UNIQUE INDEX uq_headcount_breach_open_zone "
        "ON headcount_breach_events (zone_id) WHERE status = 'open'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_headcount_breach_open_zone")
    # Same definition as created by 0006_reliability.
    op.execute(
        """
        CREATE UNIQUE INDEX ux_headcount_open_breach
        ON headcount_breach_events (zone_id)
        WHERE alert_status <> 'resolved'
        """
    )
    op.drop_column("headcount_breach_events", "status")

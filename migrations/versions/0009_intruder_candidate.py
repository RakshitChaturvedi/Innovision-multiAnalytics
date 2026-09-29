"""intruder candidates: grace-period state that survives a restart

A restricted-zone entry without an authorized winner is first stored as a
candidate (awaiting_decision = true) and only alerted after the grace
period (decide_at, event time) if it is still unresolved. candidate_since
(wall clock) drives the sweeper; zone_event_id/frame_seq let the sweeper
build the alert without the original zone event.

Revision ID: 0009_intruder_candidate
Revises: 0008_intruder_alert_cooldown
"""

from alembic import op

revision = "0009_intruder_candidate"
down_revision = "0008_intruder_alert_cooldown"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE intruder_events
            ADD COLUMN awaiting_decision BOOLEAN NOT NULL DEFAULT false,
            ADD COLUMN decide_at TIMESTAMPTZ NULL,
            ADD COLUMN candidate_since TIMESTAMPTZ NULL,
            ADD COLUMN zone_event_id UUID NULL,
            ADD COLUMN frame_seq BIGINT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_intruder_events_owed
        ON intruder_events (candidate_since)
        WHERE alert_status <> 'resolved'
          AND (awaiting_decision OR NOT alert_published)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_intruder_events_owed")
    op.execute(
        """
        ALTER TABLE intruder_events
            DROP COLUMN frame_seq,
            DROP COLUMN zone_event_id,
            DROP COLUMN candidate_since,
            DROP COLUMN decide_at,
            DROP COLUMN awaiting_decision
        """
    )

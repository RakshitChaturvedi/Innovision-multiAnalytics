"""blur recalibration: camera_config.blur_threshold on the new score scale

The recognition blur score is now resolution-independent (Laplacian variance
of the crop resized to BLUR_EVAL_SIZE px, see
services/recognition/src/quality_gate.py::blur_score). Its values are much
smaller than the old raw-crop score, so the old default of 100.0 rejects
nearly every face. Rows still at exactly 100.0 are the untouched old default
and move to the new default; any other value was set on purpose and is kept.

Downgrade restores the old server default only: rows are not rewritten,
because it can no longer tell which ones this migration changed.

Revision ID: 0009_blur_recalibration
Revises: 0008_intruder_alert_cooldown
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_blur_recalibration"
down_revision = "0008_intruder_alert_cooldown"
branch_labels = None
depends_on = None

OLD_DEFAULT = 100.0
NEW_DEFAULT = 15.0  # services/recognition/src/config.py DEFAULT_BLUR_THRESHOLD


def upgrade() -> None:
    op.alter_column(
        "camera_config", "blur_threshold",
        existing_type=sa.Float, server_default=str(NEW_DEFAULT),
    )
    op.execute(sa.text(
        "UPDATE camera_config SET blur_threshold = :new WHERE blur_threshold = :old"
    ).bindparams(new=NEW_DEFAULT, old=OLD_DEFAULT))


def downgrade() -> None:
    op.alter_column(
        "camera_config", "blur_threshold",
        existing_type=sa.Float, server_default=str(OLD_DEFAULT),
    )

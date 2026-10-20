"""Camera health worker's processing numbers for TWG (GPU, model timings, recent checks): vision_status; references
relearned (the first ones could hold people / vehicles, and day / night now follows the sun)

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'vision_status',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('data', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    # The first day's references (2026-10-20) were learned with people / a school bus in view, and against the colour
    # only: start them over with the new rules. Old running-background images go too (no longer used).
    op.execute("DELETE FROM camera_health_images WHERE kind <> 'last'")
    op.execute("UPDATE camera_health SET issues = '[]', state = '{}', learning = true, "
               "status = CASE WHEN status = 'offline' THEN 'offline' ELSE 'pending' END")


def downgrade() -> None:
    op.drop_table('vision_status')

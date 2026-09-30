"""site arming: timezone, weekly schedule, manual override

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.add_column(sa.Column('timezone', sa.String(length=64), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('arm_schedule', JSONType, nullable=True))
        batch_op.add_column(sa.Column('arm_override', JSONType, nullable=True))
        # Existing sites start armed: nothing changes until someone disarms or sets a schedule.
        batch_op.add_column(sa.Column('armed', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.drop_column('armed')
        batch_op.drop_column('arm_override')
        batch_op.drop_column('arm_schedule')
        batch_op.drop_column('timezone')

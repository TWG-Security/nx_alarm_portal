"""text messages: when each person is texted (alarm goes off | operator confirms it's real | both)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        # raised (when it goes off: everyone so far) | confirmed (when an operator marks it Real event) | both
        batch_op.add_column(sa.Column('sms_when', sa.String(length=10), nullable=False, server_default='raised'))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('sms_when')

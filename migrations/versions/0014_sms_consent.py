"""text messages: opt-in by replying YES, opt-out (STOP), help contact, alarm picture (MMS)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-12
"""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None

USER_COLS = [
    # none (never asked) | pending (asked, waiting for YES) | yes (opted in) | stopped (replied STOP)
    sa.Column('sms_consent', sa.String(length=10), nullable=False, server_default='none'),
    sa.Column('sms_consent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sms_optin_sent_at', sa.DateTime(timezone=True), nullable=True),
]
PS_COLS = [
    sa.Column('sms_help_contact', sa.String(length=300), nullable=False, server_default=''),
    sa.Column('sms_last_inbound_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sms_pictures', sa.Boolean(), nullable=False, server_default=sa.true()),
]


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        for col in USER_COLS:
            batch_op.add_column(col)
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        for col in PS_COLS:
            batch_op.add_column(col)


def downgrade() -> None:
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        for col in reversed(PS_COLS):
            batch_op.drop_column(col.name)
    with op.batch_alter_table('users', schema=None) as batch_op:
        for col in reversed(USER_COLS):
            batch_op.drop_column(col.name)

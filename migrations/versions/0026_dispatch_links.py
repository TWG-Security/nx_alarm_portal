"""Dispatch links: which alarm links were sent to dispatch a guard, with the token kept encrypted for the iPhone app

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('alarm_links') as b:
        b.add_column(sa.Column('purpose', sa.String(length=12), nullable=False, server_default=''))
        b.add_column(sa.Column('token_enc', sa.Text(), nullable=False, server_default=''))
    op.create_index('ix_alarm_links_user_purpose', 'alarm_links', ['user_id', 'purpose'])


def downgrade() -> None:
    op.drop_index('ix_alarm_links_user_purpose', 'alarm_links')
    with op.batch_alter_table('alarm_links') as b:
        b.drop_column('token_enc')
        b.drop_column('purpose')

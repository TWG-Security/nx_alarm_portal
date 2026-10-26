"""Mute a site or one camera for a few minutes: sites.mute, alarms.muted

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0034'
down_revision = '0033'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('sites') as b:
        b.add_column(sa.Column('mute', JSONType, nullable=True))
    with op.batch_alter_table('alarms') as b:
        b.add_column(sa.Column('muted', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('alarms') as b:
        b.drop_column('muted')
    with op.batch_alter_table('sites') as b:
        b.drop_column('mute')

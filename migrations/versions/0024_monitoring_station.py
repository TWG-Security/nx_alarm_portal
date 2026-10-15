"""Monitoring station: a user who is never signed out for inactivity or by the "always sign out after" limit

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-15
"""
from alembic import op
import sqlalchemy as sa

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.add_column(sa.Column('monitoring_station', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.drop_column('monitoring_station')

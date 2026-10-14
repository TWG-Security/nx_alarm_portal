"""Fleetio vehicles on the map: a tracker's id in its source system (Fleetio vehicle id)

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-14
"""
from alembic import op
import sqlalchemy as sa

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('trackers') as b:
        b.add_column(sa.Column('ext_id', sa.String(length=64), nullable=False, server_default=''))
    op.create_index('ix_trackers_source_ext_id', 'trackers', ['source', 'ext_id'])


def downgrade() -> None:
    op.drop_index('ix_trackers_source_ext_id', 'trackers')
    with op.batch_alter_table('trackers') as b:
        b.drop_column('ext_id')

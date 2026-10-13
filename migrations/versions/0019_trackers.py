"""guard tracking: Peplink InControl2 settings, trackers and their position history

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('platform_settings', schema=None) as b:
        b.add_column(sa.Column('peplink_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column('peplink_client_id', sa.String(length=200), nullable=False, server_default=''))
        b.add_column(sa.Column('peplink_secret_enc', sa.Text(), nullable=False, server_default=''))
        b.add_column(sa.Column('peplink_poll_s', sa.Integer(), nullable=False, server_default='30'))
        b.add_column(sa.Column('peplink_last_ok_at', sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column('peplink_last_error', sa.Text(), nullable=False, server_default=''))
        b.add_column(sa.Column('peplink_last_error_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'trackers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False, server_default='vehicle'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('source', sa.String(length=20), nullable=False, server_default='peplink'),
        sa.Column('ic2_org', sa.String(length=32), nullable=False, server_default=''),
        sa.Column('ic2_group', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('ic2_device', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('serial', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('device_name', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('tz', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('lat', sa.Float(), nullable=True),
        sa.Column('lng', sa.Float(), nullable=True),
        sa.Column('speed', sa.Float(), nullable=True),
        sa.Column('heading', sa.Float(), nullable=True),
        sa.Column('fix_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        'tracker_points',
        sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), primary_key=True),
        sa.Column('tracker_id', sa.Integer(), sa.ForeignKey('trackers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('lat', sa.Float(), nullable=False),
        sa.Column('lng', sa.Float(), nullable=False),
        sa.Column('speed', sa.Float(), nullable=True),
    )
    op.create_index('ix_tracker_points_tracker_ts', 'tracker_points', ['tracker_id', 'ts'])


def downgrade() -> None:
    op.drop_table('tracker_points')
    op.drop_table('trackers')
    with op.batch_alter_table('platform_settings', schema=None) as b:
        for c in ('peplink_last_error_at', 'peplink_last_error', 'peplink_last_ok_at', 'peplink_poll_s',
                  'peplink_secret_enc', 'peplink_client_id', 'peplink_enabled'):
            b.drop_column(c)

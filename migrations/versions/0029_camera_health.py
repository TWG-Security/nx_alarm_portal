"""Camera image health: camera_health, camera_health_images, camera_health_events

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'camera_health',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('site_id', sa.Integer(), sa.ForeignKey('sites.id', ondelete='CASCADE'), nullable=False),
        sa.Column('device_id', sa.String(64), nullable=False),
        sa.Column('name', sa.String(300), nullable=False, server_default=''),
        sa.Column('nx_status', sa.String(20), nullable=False, server_default=''),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('issues', JSONType, nullable=False),
        sa.Column('state', JSONType, nullable=False),
        sa.Column('metrics', JSONType, nullable=False),
        sa.Column('mode', sa.String(10), nullable=False, server_default=''),
        sa.Column('ai', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('learning', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('settings', JSONType, nullable=False),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('changed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('muted_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('check_requested_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('site_id', 'device_id', name='uq_camera_health_cam'),
    )
    op.create_index('ix_camera_health_tenant_id', 'camera_health', ['tenant_id'])
    op.create_index('ix_camera_health_changed', 'camera_health', ['changed_at'])
    op.create_table(
        'camera_health_images',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('site_id', sa.Integer(), sa.ForeignKey('sites.id', ondelete='CASCADE'), nullable=False),
        sa.Column('device_id', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('jpeg', sa.LargeBinary(), nullable=False),
        sa.Column('taken_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(20), nullable=False, server_default='auto'),
        sa.Column('by_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.UniqueConstraint('site_id', 'device_id', 'kind', name='uq_camera_health_image'),
    )
    op.create_table(
        'camera_health_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('site_id', sa.Integer(), sa.ForeignKey('sites.id', ondelete='CASCADE'), nullable=False),
        sa.Column('device_id', sa.String(64), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('kind', sa.String(30), nullable=False),
        sa.Column('change', sa.String(20), nullable=False),
        sa.Column('detail', JSONType, nullable=False),
    )
    op.create_index('ix_camera_health_events_tenant_id', 'camera_health_events', ['tenant_id'])
    op.create_index('ix_camera_health_events_cam', 'camera_health_events', ['site_id', 'device_id', 'id'])


def downgrade() -> None:
    op.drop_table('camera_health_events')
    op.drop_table('camera_health_images')
    op.drop_table('camera_health')

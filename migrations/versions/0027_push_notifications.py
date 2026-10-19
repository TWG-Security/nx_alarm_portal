"""Push notifications for the iPhone app: per-person settings, registered devices, and a send log

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.add_column(sa.Column('push_level', sa.String(length=10), nullable=True))      # NULL = not chosen: critical + alarm
        b.add_column(sa.Column('push_when', sa.String(length=10), nullable=False, server_default='raised'))
    op.create_table(
        'push_devices',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('session_id', sa.String(length=64), nullable=False, index=True),
        sa.Column('apns_token', sa.String(length=200), nullable=False, unique=True),
        sa.Column('environment', sa.String(length=10), nullable=False),
        sa.Column('app_version', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('ios_version', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_push_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        'push_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=True),
        sa.Column('alarm_id', sa.Integer(), sa.ForeignKey('alarms.id', ondelete='SET NULL'), nullable=True, index=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('device', sa.String(length=80), nullable=False, server_default=''),
        sa.Column('purpose', sa.String(length=12), nullable=False),
        sa.Column('outcome', sa.String(length=12), nullable=False),
        sa.Column('status', sa.Integer(), nullable=True),
        sa.Column('reason', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('apns_id', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('delay_ms', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('push_log')
    op.drop_table('push_devices')
    with op.batch_alter_table('users') as b:
        b.drop_column('push_when')
        b.drop_column('push_level')

"""text messages: Twilio settings, users' mobile numbers, the send log, no-sign-in alarm links

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-12
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None

USER_COLS = [
    sa.Column('phone', sa.String(length=20), nullable=False, server_default=''),
    sa.Column('sms_level', sa.String(length=10), nullable=False, server_default='off'),
]
PS_COLS = [
    sa.Column('sms_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('twilio_sid', sa.String(length=64), nullable=False, server_default=''),
    sa.Column('twilio_token_enc', sa.Text(), nullable=False, server_default=''),
    sa.Column('twilio_from', sa.String(length=64), nullable=False, server_default=''),
    sa.Column('sms_link_hours', sa.Integer(), nullable=False, server_default='24'),
    sa.Column('sms_max_per_10min', sa.Integer(), nullable=False, server_default='10'),
    sa.Column('sms_last_ok_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sms_last_error', sa.Text(), nullable=False, server_default=''),
    sa.Column('sms_last_error_at', sa.DateTime(timezone=True), nullable=True),
]


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        for col in USER_COLS:
            batch_op.add_column(col)
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        for col in PS_COLS:
            batch_op.add_column(col)
    op.create_table(
        'sms_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('to', sa.String(length=20), nullable=False),
        sa.Column('purpose', sa.String(length=10), nullable=False),
        sa.Column('outcome', sa.String(length=20), nullable=False),
        sa.Column('error', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('twilio_sid', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('delay_ms', sa.Integer(), nullable=True),
        sa.Column('alarm_id', sa.Integer(), sa.ForeignKey('alarms.id', ondelete='SET NULL'), nullable=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
    )
    op.create_index('ix_sms_log_ts', 'sms_log', ['ts'])
    op.create_index('ix_sms_log_user', 'sms_log', ['user_id', 'ts'])
    op.create_table(
        'alarm_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('token_hash', sa.String(length=64), nullable=False, unique=True),
        sa.Column('alarm_id', sa.Integer(), sa.ForeignKey('alarms.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('opened_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('open_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_index('ix_alarm_links_alarm_id', 'alarm_links', ['alarm_id'])


def downgrade() -> None:
    op.drop_index('ix_alarm_links_alarm_id', 'alarm_links')
    op.drop_table('alarm_links')
    op.drop_index('ix_sms_log_user', 'sms_log')
    op.drop_index('ix_sms_log_ts', 'sms_log')
    op.drop_table('sms_log')
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        for col in reversed(PS_COLS):
            batch_op.drop_column(col.name)
    with op.batch_alter_table('users', schema=None) as batch_op:
        for col in reversed(USER_COLS):
            batch_op.drop_column(col.name)

"""sign-in protection: platform settings, auth events, IP bans and allowlist

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'platform_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ban_max_fails', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('ban_window_min', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('ban_first_min', sa.Integer(), nullable=False, server_default='15'),
        sa.Column('ban_max_min', sa.Integer(), nullable=False, server_default='10080'),
        sa.Column('ban_permanent_after', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('account_lock_max', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('trusted_proxies', sa.Text(), nullable=False, server_default=''),
        sa.Column('cf_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('cf_zone_id', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('cf_api_token_enc', sa.Text(), nullable=False, server_default=''),
        sa.Column('cf_last_ok_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cf_last_error', sa.Text(), nullable=False, server_default=''),
        sa.Column('cf_last_error_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )
    op.execute("INSERT INTO platform_settings (id) VALUES (1)")
    op.create_table(
        'auth_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('email', sa.String(length=320), nullable=False, server_default=''),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('outcome', sa.String(length=20), nullable=False),
        sa.Column('reason', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )
    op.create_index('ix_auth_events_ip_ts', 'auth_events', ['ip', 'ts'])
    op.create_index('ix_auth_events_email_ts', 'auth_events', ['email', 'ts'])
    op.create_index('ix_auth_events_ts', 'auth_events', ['ts'])
    op.create_table(
        'ip_bans',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ip', sa.String(length=64), nullable=False, unique=True),
        sa.Column('reason', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('fail_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('ban_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('permanent', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('manual', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('cf_rule_id', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_ip_bans_expires_at', 'ip_bans', ['expires_at'])
    op.create_table(
        'ip_allowlist',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ip', sa.String(length=64), nullable=False, unique=True),
        sa.Column('label', sa.String(length=100), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('ip_allowlist')
    op.drop_index('ix_ip_bans_expires_at', 'ip_bans')
    op.drop_table('ip_bans')
    for ix in ('ix_auth_events_ts', 'ix_auth_events_email_ts', 'ix_auth_events_ip_ts'):
        op.drop_index(ix, 'auth_events')
    op.drop_table('auth_events')
    op.drop_table('platform_settings')

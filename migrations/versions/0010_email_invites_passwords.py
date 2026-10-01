"""email (SMTP settings, send log), invites and password reset links, password rules and expiry

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None

PS_COLS = [
    sa.Column('pw_min_length', sa.Integer(), nullable=False, server_default='12'),
    sa.Column('pw_upper', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('pw_lower', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('pw_number', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('pw_symbol', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('pw_expiry_days', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('portal_url', sa.String(length=300), nullable=False, server_default='https://alarmportal.twgsecurity.net'),
    sa.Column('smtp_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('smtp_host', sa.String(length=200), nullable=False, server_default=''),
    sa.Column('smtp_port', sa.Integer(), nullable=False, server_default='587'),
    sa.Column('smtp_tls', sa.String(length=10), nullable=False, server_default='starttls'),
    sa.Column('smtp_user', sa.String(length=320), nullable=False, server_default=''),
    sa.Column('smtp_password_enc', sa.Text(), nullable=False, server_default=''),
    sa.Column('smtp_from', sa.String(length=320), nullable=False, server_default=''),
    sa.Column('smtp_last_ok_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('smtp_last_error', sa.Text(), nullable=False, server_default=''),
    sa.Column('smtp_last_error_at', sa.DateTime(timezone=True), nullable=True),
]


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('invited_at', sa.DateTime(timezone=True), nullable=True))
    # Existing passwords count from when the account was made, so turning expiry on later behaves sensibly.
    op.execute("UPDATE users SET password_changed_at = created_at WHERE password_changed_at IS NULL")
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        for col in PS_COLS:
            batch_op.add_column(col)
    op.create_table(
        'user_tokens',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('purpose', sa.String(length=10), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )
    op.create_index('ix_user_tokens_user_id', 'user_tokens', ['user_id'])
    op.create_table(
        'email_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('to', sa.String(length=320), nullable=False),
        sa.Column('subject', sa.String(length=300), nullable=False),
        sa.Column('purpose', sa.String(length=20), nullable=False),
        sa.Column('outcome', sa.String(length=20), nullable=False),
        sa.Column('error', sa.String(length=500), nullable=False, server_default=''),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
    )
    op.create_index('ix_email_log_ts', 'email_log', ['ts'])


def downgrade() -> None:
    op.drop_index('ix_email_log_ts', 'email_log')
    op.drop_table('email_log')
    op.drop_index('ix_user_tokens_user_id', 'user_tokens')
    op.drop_table('user_tokens')
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        for col in reversed(PS_COLS):
            batch_op.drop_column(col.name)
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('invited_at')

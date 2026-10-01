"""two-factor (TOTP, recovery codes, passkeys) and server-side sessions

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_secret_enc', sa.Text(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('totp_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('last_totp_step', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'))
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mfa_require_twg', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('mfa_customers', sa.String(length=20), nullable=False, server_default='company'))
        batch_op.add_column(sa.Column('passkeys_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('session_closed_h', sa.Integer(), nullable=False, server_default='12'))
        batch_op.add_column(sa.Column('session_max_h', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('idle_timeout_min', sa.Integer(), nullable=False, server_default='0'))
    op.create_table(
        'mfa_recovery_codes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_mfa_recovery_codes_user_id', 'mfa_recovery_codes', ['user_id'])
    op.create_table(
        'webauthn_credentials',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('credential_id', sa.String(length=1400), nullable=False, unique=True),
        sa.Column('public_key', sa.Text(), nullable=False),
        sa.Column('sign_count', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('transports', JSONType, nullable=True),
        sa.Column('name', sa.String(length=100), nullable=False, server_default='Passkey'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('tested_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_webauthn_credentials_user_id', 'webauthn_credentials', ['user_id'])
    op.create_table(
        'user_sessions',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('method', sa.String(length=20), nullable=False, server_default='password'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('ip', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('user_agent', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_reason', sa.String(length=100), nullable=False, server_default=''),
    )
    op.create_index('ix_user_sessions_user', 'user_sessions', ['user_id', 'revoked_at'])
    op.create_index('ix_user_sessions_tenant_id', 'user_sessions', ['tenant_id'])


def downgrade() -> None:
    op.drop_index('ix_user_sessions_tenant_id', 'user_sessions')
    op.drop_index('ix_user_sessions_user', 'user_sessions')
    op.drop_table('user_sessions')
    op.drop_index('ix_webauthn_credentials_user_id', 'webauthn_credentials')
    op.drop_table('webauthn_credentials')
    op.drop_index('ix_mfa_recovery_codes_user_id', 'mfa_recovery_codes')
    op.drop_table('mfa_recovery_codes')
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        for col in ('idle_timeout_min', 'session_max_h', 'session_closed_h', 'passkeys_enabled', 'mfa_customers',
                    'mfa_require_twg'):
            batch_op.drop_column(col)
    with op.batch_alter_table('users', schema=None) as batch_op:
        for col in ('token_version', 'password_changed_at', 'last_totp_step', 'totp_enabled', 'totp_secret_enc'):
            batch_op.drop_column(col)

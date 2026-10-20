"""API tokens: a person's key for the portal's MCP server (/mcp), stored as a SHA-256 only

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'api_tokens',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('name', sa.String(length=100), nullable=False, server_default=''),
        sa.Column('token_hash', sa.String(length=64), nullable=False, unique=True),
        sa.Column('hint', sa.String(length=16), nullable=False, server_default=''),
        sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_ip', sa.String(length=64), nullable=False, server_default=''),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_reason', sa.String(length=100), nullable=False, server_default=''),
    )


def downgrade() -> None:
    op.drop_table('api_tokens')

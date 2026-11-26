"""webhooks (a company's webhooks into a site's NX: a generic event or a soft trigger, optionally a site panel button)

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0039'
down_revision = '0038'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'webhooks',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('tenant_id', sa.Integer, sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('site_id', sa.Integer, sa.ForeignKey('sites.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('enabled', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('button', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('hint', sa.String(16), nullable=False, server_default=''),
        sa.Column('config', JSONType, nullable=True),
        sa.Column('uses', sa.Integer, nullable=False, server_default='0'),
        sa.Column('recent', JSONType, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('webhooks')

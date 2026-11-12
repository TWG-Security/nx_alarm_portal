"""tunnels (a company's VPN connections to its sites: WireGuard client files, Tailscale auth keys) and sites.tunnel_id

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0038'
down_revision = '0037'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table(
        'tunnels',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('tenant_id', sa.Integer, sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('enabled', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('secret_enc', sa.Text, nullable=False),
        sa.Column('info', JSONType, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_tunnels_tenant_name'),
    )
    with op.batch_alter_table('sites') as b:
        b.add_column(sa.Column('tunnel_id', sa.Integer, nullable=True))
        b.create_foreign_key('fk_sites_tunnel_id', 'tunnels', ['tunnel_id'], ['id'])
        b.create_index('ix_sites_tunnel_id', ['tunnel_id'])


def downgrade() -> None:
    with op.batch_alter_table('sites') as b:
        b.drop_index('ix_sites_tunnel_id')
        b.drop_constraint('fk_sites_tunnel_id', type_='foreignkey')
        b.drop_column('tunnel_id')
    op.drop_table('tunnels')

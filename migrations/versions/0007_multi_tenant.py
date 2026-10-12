"""multi-tenant: tenant kind (platform/customer), active flag, branding

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=20), nullable=False, server_default='customer'))
        batch_op.add_column(sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column('display_name', sa.String(length=200), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('logo', sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column('logo_type', sa.String(length=50), nullable=False, server_default=''))
    # The first tenant is the operator of the portal (TWG Security): it becomes the platform.
    op.execute("UPDATE tenants SET kind = 'platform' WHERE id = (SELECT MIN(id) FROM tenants)")


def downgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        for col in ('logo_type', 'logo', 'display_name', 'is_active', 'kind'):
            batch_op.drop_column(col)

"""Google sign-in (OIDC) settings

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('google_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('google_client_id', sa.String(length=300), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('google_client_secret_enc', sa.Text(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('google_domains', sa.String(length=500), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('platform_settings', schema=None) as batch_op:
        for col in ('google_domains', 'google_client_secret_enc', 'google_client_id', 'google_enabled'):
            batch_op.drop_column(col)

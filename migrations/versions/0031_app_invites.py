"""Phone apps: TestFlight public link and App Store Connect settings for TestFlight invites (platform_settings)

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa

revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('platform_settings') as b:
        b.add_column(sa.Column('testflight_url', sa.String(300), nullable=False, server_default=''))
        b.add_column(sa.Column('asc_issuer_id', sa.String(64), nullable=False, server_default=''))
        b.add_column(sa.Column('asc_app_id', sa.String(32), nullable=False, server_default=''))
        b.add_column(sa.Column('asc_beta_group', sa.String(100), nullable=False, server_default='TWG Testers'))


def downgrade() -> None:
    with op.batch_alter_table('platform_settings') as b:
        for c in ('asc_beta_group', 'asc_app_id', 'asc_issuer_id', 'testflight_url'):
            b.drop_column(c)

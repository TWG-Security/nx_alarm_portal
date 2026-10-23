"""Two-step sign-in turned off for one person: users.mfa_exempt

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-23
"""
from alembic import op
import sqlalchemy as sa

revision = '0032'
down_revision = '0031'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.add_column(sa.Column('mfa_exempt', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.drop_column('mfa_exempt')

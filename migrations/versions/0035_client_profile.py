"""Client profile per site: contacts, package (ground response / monitoring only), police number, notes

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('sites') as b:
        b.add_column(sa.Column('profile', JSONType, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sites') as b:
        b.drop_column('profile')

"""tracker_sources.options: a Geotab database's name and server

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0036'
down_revision = '0035'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('tracker_sources') as b:
        b.add_column(sa.Column('options', JSONType, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tracker_sources') as b:
        b.drop_column('options')

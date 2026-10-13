"""sites: 3D model shown in the 3D view (upload, conversion job, placement)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model', JSONType, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.drop_column('model')

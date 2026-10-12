"""camera positions on the site's 3D view

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('sites', schema=None) as batch_op:
        # {"<NX device id>": {"name", "lat", "lng", "heading", "fov", "range_m"}}
        batch_op.add_column(sa.Column('camera_places', JSONType, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.drop_column('camera_places')

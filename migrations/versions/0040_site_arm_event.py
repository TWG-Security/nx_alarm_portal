"""sites.arm_event: send a Generic Event to the site's NX when it arms or disarms (source, captions, on/off)

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0040'
down_revision = '0039'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('sites') as b:
        b.add_column(sa.Column('arm_event', JSONType, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sites') as b:
        b.drop_column('arm_event')

"""Alarms during an active call join that call: alarms.call_id = the call's alarm

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('alarms') as b:
        b.add_column(sa.Column('call_id', sa.Integer(), sa.ForeignKey('alarms.id', ondelete='SET NULL', name='fk_alarms_call_id'), nullable=True))
    op.create_index('ix_alarms_call_id', 'alarms', ['call_id'])


def downgrade() -> None:
    op.drop_index('ix_alarms_call_id', 'alarms')
    with op.batch_alter_table('alarms') as b:
        b.drop_constraint('fk_alarms_call_id', type_='foreignkey')
        b.drop_column('call_id')

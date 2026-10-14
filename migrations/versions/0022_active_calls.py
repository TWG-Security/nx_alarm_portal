"""active calls: a dispatched alarm stays open (as a call) until someone ends it

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('alarms') as b:
        b.add_column(sa.Column('call_state', sa.String(length=10), nullable=False, server_default=''))
        b.add_column(sa.Column('call_ended_at', sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column('call_ended_by_id', sa.Integer(), sa.ForeignKey('users.id', name='fk_alarms_call_ended_by'), nullable=True))
        b.add_column(sa.Column('call_end_note', sa.Text(), nullable=False, server_default=''))
    op.create_index('ix_alarms_call_state', 'alarms', ['call_state'])


def downgrade() -> None:
    op.drop_index('ix_alarms_call_state', 'alarms')
    with op.batch_alter_table('alarms') as b:
        b.drop_constraint('fk_alarms_call_ended_by', type_='foreignkey')
        for c in ('call_end_note', 'call_ended_by_id', 'call_ended_at', 'call_state'):
            b.drop_column(c)

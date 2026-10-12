"""follow-up notes on alarms (append-only)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'alarm_notes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('alarm_id', sa.Integer(), sa.ForeignKey('alarms.id'), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_alarm_notes_alarm', 'alarm_notes', ['alarm_id', 'id'])


def downgrade() -> None:
    op.drop_index('ix_alarm_notes_alarm', table_name='alarm_notes')
    op.drop_table('alarm_notes')

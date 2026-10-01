"""alarm verdicts (real / false alarm); user groups with permissions

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    with op.batch_alter_table('alarms', schema=None) as batch_op:
        batch_op.add_column(sa.Column('verdict', sa.String(length=10), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('verdict_by_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('verdict_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key('fk_alarms_verdict_by', 'users', ['verdict_by_id'], ['id'])
    op.create_table(
        'user_groups',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('permissions', JSONType, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_groups_tenant_name'),
    )
    op.create_table(
        'user_group_members',
        sa.Column('group_id', sa.Integer(), sa.ForeignKey('user_groups.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True, index=True),
    )


def downgrade() -> None:
    op.drop_table('user_group_members')
    op.drop_table('user_groups')
    with op.batch_alter_table('alarms', schema=None) as batch_op:
        batch_op.drop_constraint('fk_alarms_verdict_by', type_='foreignkey')
        batch_op.drop_column('verdict_at')
        batch_op.drop_column('verdict_by_id')
        batch_op.drop_column('verdict')

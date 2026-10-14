"""guards: the person to text (dispatch), router / phone online state, browser location-share links

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('trackers') as b:
        b.add_column(sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', name='fk_trackers_user_id', ondelete='SET NULL'), nullable=True))
        b.add_column(sa.Column('online', sa.Boolean(), nullable=True))
        b.add_column(sa.Column('offline_since', sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column('share_hash', sa.String(length=64), nullable=False, server_default=''))
        b.add_column(sa.Column('share_enc', sa.Text(), nullable=False, server_default=''))
        b.add_column(sa.Column('share_expires_at', sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column('share_seen_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_trackers_share_hash', 'trackers', ['share_hash'])


def downgrade() -> None:
    op.drop_index('ix_trackers_share_hash', 'trackers')
    with op.batch_alter_table('trackers') as b:
        for c in ('share_seen_at', 'share_expires_at', 'share_enc', 'share_hash', 'offline_since', 'online', 'user_id'):
            if c == 'user_id':
                b.drop_constraint('fk_trackers_user_id', type_='foreignkey')
            b.drop_column(c)

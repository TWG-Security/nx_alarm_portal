"""guard tracking per company: each company's own InControl2 API client (moved from the Platform page)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'tracker_sources',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id'), nullable=False, index=True),
        sa.Column('kind', sa.String(length=20), nullable=False, server_default='peplink'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('client_id', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('secret_enc', sa.Text(), nullable=False, server_default=''),
        sa.Column('poll_s', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=False, server_default=''),
        sa.Column('last_error_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('tenant_id', 'kind', name='uq_tracker_sources_tenant_kind'),
    )
    # The credentials entered on the Platform page belong to TWG (the platform company) from now on.
    conn = op.get_bind()
    row = conn.execute(sa.text("select peplink_enabled, peplink_client_id, peplink_secret_enc, peplink_poll_s "
                               "from platform_settings where id = 1")).first()
    twg = conn.execute(sa.text("select id from tenants where kind = 'platform' order by id limit 1")).first()
    if row is not None and twg is not None and (row[1] or row[2]):
        conn.execute(sa.text("insert into tracker_sources (tenant_id, kind, enabled, client_id, secret_enc, poll_s, last_error, updated_at) "
                             "values (:t, 'peplink', :e, :c, :s, :p, '', CURRENT_TIMESTAMP)"),
                     {"t": twg[0], "e": bool(row[0]), "c": row[1] or "", "s": row[2] or "", "p": row[3] or 30})


def downgrade() -> None:
    op.drop_table('tracker_sources')

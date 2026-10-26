"""audit_log.client / client_version and user_sessions.client / client_version: iPhone app, Android app or website

Sign-ins and sign-outs already recorded say which (detail.method "app (…)" / detail.app), so they're filled in;
other older rows stay "" (not recorded then).

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-26
"""
from alembic import op
import sqlalchemy as sa

revision = '0037'
down_revision = '0036'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ('audit_log', 'user_sessions'):
        with op.batch_alter_table(table) as b:
            b.add_column(sa.Column('client', sa.String(16), nullable=False, server_default=''))
            b.add_column(sa.Column('client_version', sa.String(40), nullable=False, server_default=''))
    bind = op.get_bind()
    audit = sa.table('audit_log', sa.column('id', sa.Integer), sa.column('action', sa.String),
                     sa.column('detail', sa.JSON), sa.column('client', sa.String))
    apps, web = [], []
    for rid, detail in bind.execute(sa.select(audit.c.id, audit.c.detail)
                                    .where(audit.c.action.in_(['login', 'login.failed', 'logout']))):
        d = detail if isinstance(detail, dict) else {}
        (apps if d.get('app') or str(d.get('method') or '').startswith('app (') else web).append(rid)
    for kind, ids in (('app', apps), ('web', web)):
        for i in range(0, len(ids), 500):
            bind.execute(audit.update().where(audit.c.id.in_(ids[i:i + 500])).values(client=kind))
    sessions = sa.table('user_sessions', sa.column('method', sa.String), sa.column('client', sa.String))
    bind.execute(sessions.update().where(sessions.c.method == 'mobile').values(client='app'))
    bind.execute(sessions.update().where(sessions.c.method != 'mobile').values(client='web'))


def downgrade() -> None:
    for table in ('audit_log', 'user_sessions'):
        with op.batch_alter_table(table) as b:
            b.drop_column('client_version')
            b.drop_column('client')

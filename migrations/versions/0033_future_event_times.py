"""Alarms a camera stamped in the future are dated when they reached the portal

Cameras at 2 Rothmere Dr stamped some camera-side analytics events a day ahead (2026-10-24..25). Their clips
waited for "tomorrow" and never played. From now on ingest dates such events on arrival (poller.FUTURE_SKEW_MS);
this does the same for the rows already stored. The raw NX row keeps the camera's time.
Also pulls any poll cursor that ran ahead of the clock back to now.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-26
"""
import time
from datetime import timezone

from alembic import op
import sqlalchemy as sa

revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None

SKEW_MS = 60_000


def upgrade() -> None:
    bind = op.get_bind()
    alarms = sa.table('alarms', sa.column('id', sa.Integer), sa.column('event_ts_ms', sa.BigInteger),
                      sa.column('received_at', sa.DateTime(timezone=True)))
    for aid, ts, received in bind.execute(sa.select(alarms.c.id, alarms.c.event_ts_ms, alarms.c.received_at)
                                          .where(alarms.c.event_ts_ms > 0)).all():
        if received is None:
            continue
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        got = int(received.timestamp() * 1000)
        if ts > got + SKEW_MS:
            bind.execute(alarms.update().where(alarms.c.id == aid).values(event_ts_ms=got))
    sites = sa.table('sites', sa.column('event_cursor_ms', sa.BigInteger))
    now = int(time.time() * 1000)
    bind.execute(sites.update().where(sites.c.event_cursor_ms > now).values(event_cursor_ms=now))


def downgrade() -> None:
    pass        # the camera's time is still in alarms.raw if it's ever needed

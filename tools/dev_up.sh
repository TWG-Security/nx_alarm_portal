#!/bin/bash
# Local test stack: fake NX on :8199 + portal (SQLite) on :8099, fresh database.
# Dev login: admin@twgsecurity.com / Smoke-test-password-123!  (dev DB only)
# Needs .env.dev (SECRET_KEY, FERNET_KEY, DATABASE_URL=sqlite+aiosqlite:///./dev.db, COOKIE_SECURE=false).
# Env overrides: POLL_INTERVAL_S (default 5; set 60 to prove push), CLIP_PRE_S / CLIP_POST_S (default 5 / 6).
set -e
cd "$(dirname "$0")/.."
./tools/dev_down.sh >/dev/null 2>&1 || true
set -a; . ./.env.dev; set +a
rm -rf dev.db media-cache tile-cache
.venv/bin/alembic upgrade head >/dev/null
echo 'Smoke-test-password-123!' | .venv/bin/python -m app.cli create-admin --email admin@twgsecurity.com --name "Dev Admin" --password-stdin >/dev/null
mkdir -p .dev-logs
nohup .venv/bin/uvicorn tools.fake_nx:app --port 8199 > .dev-logs/fake_nx.log 2>&1 &
POLL_INTERVAL_S=${POLL_INTERVAL_S:-5} CLIP_PRE_S=${CLIP_PRE_S:-5} CLIP_POST_S=${CLIP_POST_S:-6} \
  WEBAUTHN_RP_ID=localhost WEBAUTHN_ORIGINS=http://localhost:8099 \
  nohup .venv/bin/uvicorn app.main:app --port 8099 > .dev-logs/portal.log 2>&1 &
for i in $(seq 1 30); do curl -sf localhost:8099/healthz >/dev/null && curl -sf localhost:8199/rest/v4/site/info >/dev/null && break; sleep 0.5; done
echo "dev stack up: portal http://localhost:8099  fake NX http://localhost:8199  (logs in .dev-logs/)"

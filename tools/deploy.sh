#!/bin/bash
# Deploy the committed code to production in one step (the user, 2026-10-19: Claude may deploy without asking each
# time; Claude still says in its reply that it's deploying, and when the portal is back).
#
#   1. refuse with uncommitted changes (production must always be a commit)
#   2. tools/backup.sh (database + .env, restore-verified)
#   3. docker compose up -d --build app (migrations run on start), then the camera health worker (`vision`, its own
#      image; a failure there never fails the deploy: the alarm pipeline doesn't depend on it)
#   4. wait for the container's health check, then for "live push connected" from every site that connected
#      before the restart
#   5. print a summary; exit 1 if the portal didn't come back healthy (alarms then rely on nothing: act at once)
#
# Usage: tools/deploy.sh            (from anywhere; allow-listed as /home/twg/nx_alarm_portal/tools/deploy.sh)
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "DEPLOY REFUSED: uncommitted changes. Commit them first:"; git status --short --untracked-files=no; exit 1
fi
sha=$(git rev-parse --short HEAD)
was=$(docker compose logs app --since 72h 2>/dev/null | grep -oE "site [0-9]+: live push connected" | grep -oE "site [0-9]+" | sort -u | tr '\n' ' ')
echo "== deploying $sha (sites with live push before: ${was:-none seen})"

echo "== backup"
backup=$(tools/backup.sh 2>&1 | tail -1)
echo "${backup:0:110}"
if ! echo "$backup" | grep -q "^backup ok"; then
  echo "DEPLOY STOPPED: the backup failed (nothing was restarted)."; exit 1
fi

echo "== build and restart"
t0=$(date +%s)
since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if ! docker compose up -d --build app 2>&1 | tail -4; then
  echo "DEPLOY FAILED: docker compose up returned an error."; exit 1
fi

echo "== waiting for health"
health=""
for _ in $(seq 1 60); do
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$(docker compose ps -q app)" 2>/dev/null)
  [ "$health" = "healthy" ] && break
  sleep 2
done
logs=$(docker compose logs app --since "$since" 2>&1)
if [ "$health" != "healthy" ]; then
  echo "DEPLOY FAILED: the app is '$health' after 120 s. Last log lines:"; echo "$logs" | tail -30
  echo "Roll back: git checkout <previous sha> && tools/deploy.sh (then return to the branch)."; exit 1
fi
echo "healthy after $(( $(date +%s) - t0 )) s"

echo "== camera health worker"
# --no-deps: never let Compose rebuild / recreate `app` as vision's dependency (2026-10-20: a vision-only
# `docker compose up -d --build vision` rebuilt the app image and restarted the portal without warning).
if docker compose build vision 2>&1 | tail -1 && docker compose up -d --no-deps vision 2>&1 | tail -2; then
  sleep 5
  docker compose logs vision --since "$since" 2>&1 | grep -E "camera health worker|GPU|no GPU|Traceback" | tail -3 | sed 's/^vision-1  | //'
else
  echo "WARNING: the vision worker didn't start (camera health only; alarms are unaffected)"
fi

echo "== waiting for live push"
for _ in $(seq 1 30); do
  logs=$(docker compose logs app --since "$since" 2>&1)
  now=$(echo "$logs" | grep -oE "site [0-9]+: live push connected" | grep -oE "site [0-9]+" | sort -u | tr '\n' ' ')
  missing=$(for s in $(echo "$was" | grep -oE "[0-9]+"); do echo "$now" | grep -qw "site $s" || echo -n "site $s "; done)
  [ -z "$missing" ] && [ -n "$now" -o -z "$was" ] && break
  sleep 2
done
echo "$logs" | grep -E "Running upgrade|live push connected" | sed 's/^app-1  | //'
errors=$(echo "$logs" | grep -cE "ERROR|Traceback")
echo "== $sha deployed in $(( $(date +%s) - t0 )) s: health $health, live push: ${now:-none}${missing:+ (STILL WAITING: $missing; polling covers them)}, errors in log: $errors"
[ "$errors" -gt 0 ] && echo "$logs" | grep -E "ERROR|Traceback" | tail -10
exit 0

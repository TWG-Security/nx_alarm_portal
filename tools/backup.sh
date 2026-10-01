#!/bin/bash
# Back up the portal: Postgres (custom-format dump) + .env (holds FERNET_KEY, without which the
# stored NX site passwords can't be decrypted) + checksums. Verifies the dump by restoring it into
# a scratch database and comparing row counts. Keeps the newest $KEEP backups.
#
#   tools/backup.sh                 # -> ~/backups/nx_alarm_portal/<timestamp>/
#   BACKUP_DIR=/mnt/x KEEP=30 tools/backup.sh
#
# Restore:  docker compose exec -T db pg_restore -U portal -d portal --clean --if-exists < portal.dump
#           (stop the app first: docker compose stop app), then put env.backup back as .env.
set -euo pipefail
cd "$(dirname "$0")/.."
BACKUP_DIR=${BACKUP_DIR:-$HOME/backups/nx_alarm_portal}
KEEP=${KEEP:-14}
# Old shells may not have the docker group yet; fall back to sg (the whole command as one string).
if docker info >/dev/null 2>&1; then dc() { docker compose "$@"; }
else dc() { sg docker -c "docker compose $(printf '%q ' "$@")"; }; fi
fail() { echo "BACKUP FAILED: $*" >&2; exit 1; }

stamp=$(date -u +%Y%m%d-%H%M%SZ)
dest="$BACKUP_DIR/$stamp"
umask 077
mkdir -p "$dest"

dc exec -T db pg_dump -U portal -d portal -Fc > "$dest/portal.dump"
[ "$(head -c 5 "$dest/portal.dump")" = "PGDMP" ] || fail "$dest/portal.dump is not a Postgres dump"
cp .env "$dest/env.backup"
git rev-parse HEAD > "$dest/git-commit.txt" 2>/dev/null || true

# Verify: restore into a scratch database and compare row counts table by table.
exact="select string_agg(format('%s=%s', table_name,
  (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I', table_name), false, true, '')))[1]::text),
  ' ' order by table_name) from information_schema.tables where table_schema = 'public'"
dc exec -T db psql -U portal -d postgres -qc "drop database if exists portal_restore_check" >/dev/null
dc exec -T db psql -U portal -d postgres -qc "create database portal_restore_check" >/dev/null
dc exec -T db pg_restore -U portal -d portal_restore_check --no-owner < "$dest/portal.dump"
# The SQL goes in on stdin: the sg fallback runs commands through sh, which can't take it as an argument.
live=$(printf '%s' "$exact" | dc exec -T db psql -U portal -d portal -tA)
restored=$(printf '%s' "$exact" | dc exec -T db psql -U portal -d portal_restore_check -tA)
dc exec -T db psql -U portal -d postgres -qc "drop database portal_restore_check" >/dev/null
[[ "$restored" == *"alarms="* && "$restored" == *"sites="* ]] || fail "restore check returned no tables: '$restored'"
echo "$restored" > "$dest/row-counts.txt"
if [ "$live" != "$restored" ]; then
  # Rows can arrive between the dump and the check (alarms, audit); report, don't fail silently.
  echo "NOTE: live and restored row counts differ (new rows since the dump?):" >&2
  echo "  live:     $live" >&2
  echo "  restored: $restored" >&2
fi

(cd "$dest" && sha256sum portal.dump env.backup git-commit.txt row-counts.txt > SHA256SUMS)
# Rotation: only timestamped folders directly inside BACKUP_DIR are ever removed.
[[ -n "$BACKUP_DIR" && "$BACKUP_DIR" != "/" && -d "$BACKUP_DIR" ]] || fail "bad BACKUP_DIR '$BACKUP_DIR'"
ls -1dt "$BACKUP_DIR"/20[0-9][0-9][01][0-9][0-3][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]Z/ 2>/dev/null \
  | tail -n +$((KEEP + 1)) | xargs -r rm -r --
echo "backup ok: $dest ($(du -sh "$dest" | cut -f1)); restored rows: $restored"

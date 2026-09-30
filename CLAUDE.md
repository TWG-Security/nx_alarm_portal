# TWG Alarm Portal: project brief for Claude

Read this at the start of every session. It's the handoff: what exists, how it works, what NX actually
does (verified, not assumed), how to deploy and test, the rules, and what's next. **Update it at the end of
any session that changes the picture.** Keep it current rather than adding a history log.

## What this is
A central alarm-monitoring portal for **TWG Security** (commercial security integrator, dozens of customer
sites on **Nx Witness 6.1**). Operators see every site on a map, get alarms from all sites in one live feed,
watch the incident clip, acknowledge with a note, and every step is audit-logged.

- **Repo:** `github.com/TWG-Security/nx_alarm_portal`. Work happens on branch `feature/base-portal`, and **PR #1** is open against `main` (not merged).
  Push with plain `git push`; the credentials are a classic PAT in `~/.git-credentials` (see Open items).
- **Server:** Ubuntu 24.04, `10.1.10.97`, 2 vCPU / 4 GB. User `twg` has passwordless sudo and is in the `docker` group.
- **Live URL:** `https://10.1.10.97`, via Caddy with an internal CA, so browsers show a certificate warning.
  **Public:** `https://alarmportal.twgsecurity.net` through a Cloudflare Tunnel the user set up on 2026-09-30. The cloudflared connector is **not** on this server. It connects to Caddy on :443 with the tunnel hostname as Host, so that name must be in the Caddyfile site list (`{$PUBLIC_HOST:alarmportal.twgsecurity.net}`). Otherwise Caddy answers an **empty 200**, which is a white page; that was the case until 2026-09-30.
  Measured through the tunnel: alarms arrive **+3 ms vs LAN**, the SSE stream isn't buffered, and the longest silence is 5.0 s. Password login is the only gate so far (see Open items).
- **The NX API client** (`app/nx/client.py`) is copied from `github.com/TWG-Security/nx-witness-mcp`, with its local changes listed in the file header.
  The claude.ai **NX_Witness MCP** (TWG MCP Gateway) talks to the same systems: `TWG`, `Bethel_Church`, `MedEvac`, `SecTV`, `TheWaterfront`. It's handy for probing NX, and `nx_write_fire_trigger` fires test soft triggers.

## The user
- They run TWG's systems and want progress, not questionnaires. Pick sensible defaults and state them; ask only real blockers.
- When they have to do something (GitHub settings, NX config, sudo), give **numbered, click-level steps**, including exactly where to type commands.
- **Delayed or silently missed alarms are unacceptable. Their words: "NEVER, this is going to be critical".** Every change must keep sub-second delivery and loud failure modes. **Test failure paths end to end and measure the latency** before saying something works. Never claim timing you haven't measured.
- **Tell the user before redeploying.** A restart once landed on their test press.

## Current production state (2026-09-30)
- **Production runs commit `5108781`** (deployed 2026-09-30 18:00 UTC): site arming plus NX rule health. **Arming was verified on production.** A Truss 8 press while site 1 was disarmed arrived in 17 ms and was stored as "disarmed" (alarm 41), with nothing sent to browsers. Both sites are armed, with no schedules.
- **NX rule health:** `poller.rule_delays` flags enabled alarm-level rules with `action.intervalS > 0` (NX merges repeats inside the interval and writes them when it ends). The map site panel and Sites page show it, along with "can't read rules".
- **The Truss 8 rule's "Interval of action" was 60 s** (repeat presses measured 35 s and 60.6 s late). The user OK'd turning it off, and it has been `intervalS: 0` since 2026-09-30. Verified at 18:00 UTC: two presses 1 s apart arrived 15 ms and 23 ms after their timestamps.
- Sites:
  - **#1 "TWG Security Office"**: Nx Cloud relay `2bc0aef4-cf2f-4f3e-9303-e05c7d1345f8`. Its NX server is also reachable on the LAN at `https://10.1.29.162:7001`.
  - **#2 "Mikey Home Beta"**: relay `c306458c-5f9c-494b-8cb6-d9530ecc0ca8`. It has a CVEDIA plugin.
- A temporary operator `e2e-probe@twgsecurity.com` exists but is **deactivated**. Reactivate it with a new password for `tools/e2e/prod_probe.py`, then deactivate it again.
- Admin login: `msupczenski@twgsecurity.com`. The temporary password is in `~/portal-admin-temp-password.txt` (mode 600). The user should change it and delete the file.
- The user **turned off the camera ONVIF analytics rules** on site 1; only soft triggers remain there.
  The Truss 8 soft trigger rule (id `79239a08-fb91-4b89-a6ca-6562ee72639f`, trigger `256dacab-d698-4a32-b9ec-135ba73ba127`,
  device `3c78795b-9824-22c1-88ad-cdb477f27197`) is tagged `#warning` and its action is "Write to Log". It's the quiet one to fire for tests.
- Measured on production:
  - soft trigger → portal: **87–219 ms**, by push
  - video playing **6.9 s** after the event
  - a 30 s clip (including conversion to H.264) is ready in about 2 s

## Architecture (where things live)
```
NX site ─┬─ push: JSON-RPC wss /jsonrpc  rest.v4.events.log.subscribe ─┐  app/services/push.py
         └─ poll: GET /rest/v4/events/log (backstop)                   ─┤  app/services/poller.py
                     ingest (dedupe by event_key, per-site lock) ───────┘  -> Postgres -> bus -> SSE -> browsers
```
| Area | Files |
|---|---|
| Settings (all env/.env) | `app/config.py` |
| Models: tenants, users, sites, alarms, audit_log | `app/models.py` (every table has `tenant_id`); migrations in `migrations/versions/0001-0003` |
| Alarm levels (critical 1 / alarm 2 / warning 3 / ignore) | `app/services/alarm_filter.py` |
| NX row → portal fields, captions | `app/services/nx_events.py` |
| Pollers, ingest, rule `#tags`, rule health (`rule_delays`), site status, "site connection lost" alarm | `app/services/poller.py` |
| NX push (JSON-RPC websocket) | `app/services/push.py` |
| Acknowledge + NX write-back (forced-ack clear or bookmark) | `app/services/ack.py` |
| Site arming: state maths, schedules, `#24h`, announce loop | `app/services/arming.py` (migration 0004); API `POST /api/sites/{id}/arm|disarm` |
| Clips (growing → full), ffmpeg, cache, prefetch, bounding boxes | `app/services/clips.py` |
| SSE stream, snapshot/clip/objects endpoints | `app/routers/stream.py` |
| JSON API (sites, alarms, audit, users, settings, geocode) | `app/routers/api.py` |
| OSM tile proxy + disk cache | `app/routers/tiles.py` |
| Frontend (no build step) | `app/templates/*.html`, `app/static/js/*.js`, `app/static/css/theme.css`, Leaflet vendored in `app/static/vendor/leaflet` |

JS modules:
- `common.js`: API, the open-alarm store, the SSE connection with reconnect, watchdog and fallback polling
- `console.js`: entry point on every page
- `sound.js`: siren/chime/tones, silence, mute, one "leader" tab plays
- `critical.js`: the pop-up
- `drawer.js`: alarm details
- `player.js`: clip player, timeline and boxes
- `map.js`: overview with sites, map and live feed
- `alarms.js`, `sites.js`, `site_form.js`, `settings.js`, `audit.js`, `users.js`

### How an alarm's level is decided (first match wins)
1. A `#critical` / `#alarm` / `#warning` / `#ignore` tag in the NX rule's Title/Comment. Rules are re-read every 60 s, and still-open alarms are re-levelled.
2. The NX rule has "Force acknowledgement" ticked → critical.
3. Per-site override (`sites.alarm_types`). This has **no UI yet**.
4. Tenant setting per event type (the Settings page).
5. Built-in default (`EVENT_TYPES`); unknown event types → warning.

`alarms.level_source` records which rule applied, and the details drawer shows it.

### Arming (per site)
- **Portal-side only; NX rules are untouched.** While a site is disarmed, **security** events are still ingested and stored with `alarms.state = "disarmed"`. They are not raised: no feed card, sound or pop-up, and they are not open or ackable. They're listed under Alarms → "While disarmed".
  System-category events (site offline, storage, server failure, unknown types) always raise, and so does any NX rule with **`#24h`** in its Title/Comment.
- The state is a **pure function** `arming.state_at(schedule, override, tz, t)`. The latest of these at or before `t` wins:
  - weekly entries in the site time zone
  - the last manual arm/disarm
  - a disarm timer's expiry (dropped if a scheduled change comes first)
  With none of them, the site is armed. Ingest computes it per event, so **alarm correctness never depends on a background job**. An event raises if the site was armed at event time **or** on arrival.
- `sites.armed` is only the last *announced* state. `run_scheduler` (1 s tick, started in `main.lifespan`) spots flips, writes `site.armed`/`site.disarmed` to the audit log and publishes `site.updated`.
- **A schedule edit never flips the state:** the save stores the current state as an override and sets `schedule.since_ms`.
- NX pushes one row per rule, so a `#24h` row arriving after another rule's row promotes the stored "disarmed" alarm to new (audited as `alarm.raised`).
- `#24h` needs rule-read rights on the NX account, which site 2 lacks today.
- Operators can arm and disarm; the schedule is edited on the admin site form. The default time zone is `DEFAULT_TIMEZONE` (America/New_York); a new site's form pre-fills the browser's zone.

### Delivery guarantees (keep these intact)
- **NX → portal:** push (about 0.1 s). The poll backstop runs every 5 s, and **every 1 s while push is down**. A failed poll retries after 1 s.
  A site is only marked offline after 60 s of failures; a bad password shows at once. Going offline raises a "Site connection lost" alarm.
- **Portal → browser:** SSE with a 5 s server ping and a 12 s watchdog, reconnecting on any error (EventSource gives up for good on HTTP errors like the 502 during restarts).
  While the stream is down the page polls open alarms every 2 s. After 10 s it shows a red "LIVE UPDATES LOST" banner and plays a tone every 10 s.
  Alarms found by any catch-up path are raised like live ones.
- Single uvicorn worker (the pollers and bus live in-process), with `--timeout-graceful-shutdown 2`.

## NX 6.1 facts we verified (don't re-learn these)
**Push (JSON-RPC)**
- Connect: `POST /rest/v4/login/tickets` → `wss://<host>/jsonrpc?_ticket=<token>`. This works through the vmsproxy relay.
- `rest.v4.events.log.subscribe` then delivers `rest.v4.events.log.update` notifications, one per new row.
- `startTimeMs` is **ignored** over JSON-RPC. Without `limit`, NX returns its whole log (104k rows / 145 MB on site 1), so we send `limit: 1`.
- NX still takes **about 10 s** server-side to set a subscription up.
- `rest.v4.analytics.subscribe` fails ("Integration has failed to subscribe"), and `rest.v4.events.subscribe` doesn't exist.

**Event log**
- One NX event produces one row per rule action. The portal collapses them by `event_key` and keeps the loudest classification.
- NX only logs events that some rule fired on. A rule's **"Interval of action"** makes NX hold back repeats inside that interval, so it should be off on alarm rules.

**Clips**
- `GET /rest/v4/devices/{id}/media.mp4?positionMs&durationMs&stream=secondary` starts on the keyframe **before** `positionMs`. The true start time is in the MP4 comment tag `{"startTimeMs": ...}` (read with ffprobe).
- `utcTimestamps` changes nothing visible.
- `accurateSeek` forces NX to transcode, so we don't use it.
- `videoCodec=h264&resolution=720p` makes NX transcode (3–11 s per 10 s clip); that's our "HD". `videoCodec=libx264` → 415.
- Site 1 records **H.265**. Site 2's secondary stream is **MPEG-4 Part 2** (browsers can't play it); its primary is 4K H.265. The portal converts to H.264 with ffmpeg.
- Archive is served up to about 1 s behind live, but requests ending that close **wait in real time** (6–7 s). Ending 3 s back returns in 1–3 s, which drives the growing-clip timing.

**Bounding boxes**
- `GET /rest/v4/analytics/objectTracks?deviceId&startTimeMs&endTimeMs`, then `/objectTracks/{id}/objectMetadata?deviceId=`, gives per-frame `boundingBox` `"x,y,WxH"` (0..1), about 6 per second for CVEDIA.
- Camera-side ONVIF rule-engine events have **no tracks**, so there's nothing to draw for them.

**Latency upstream of the portal**
- Camera ONVIF analytics events carry whole-second camera time stamps and reached the portal 2–8 s late, occasionally up to a minute. This looks like the camera → NX path.
- It was **not confirmed** with the rules re-enabled, because the user turned them off. The portal logs `pushed <type> N ms after its timestamp` for every push, so re-check when they're back.

**Other**
- The vmsproxy relay returns frequent one-off **HTTP 503**s (about one poll in three at times). Push works around them.
- Site 2's NX account gets **403 on `/rest/v4/events/rules`**, so `#tags` don't work there. It needs rule-read rights (e.g. the Power Users group).
- NX servers use self-signed certificates, so `verify=False`.
- OSM tiles got "Access blocked" in real browsers because our Referrer-Policy suppresses the Referer header. That's why tiles go through `/tiles/` with an identifying User-Agent.

## Deploy
```bash
cd ~/nx_alarm_portal
docker compose up -d --build app        # rebuild + restart the app only (db/caddy keep running); migrations run on start
docker compose logs app --since 5m      # look for "live push connected" for every site
docker compose exec -T db psql -U portal -d portal -c "select id,name,status from sites"
```
- **Warn the user first:** a restart makes push re-subscribe (about 10 s, covered by 1 s polling).
- Secrets live in `.env` (mode 600, gitignored): SECRET_KEY, FERNET_KEY, POSTGRES_PASSWORD, PORTAL_HOST. Never print them.
- Docker volumes: `pgdata`, `tilecache`, `mediacache` (clips, 4 GB LRU cap), plus Caddy's volumes.
- If `docker` says permission denied in an old shell, use `sg docker -c "..."`.

## Develop and test
```bash
cd ~/nx_alarm_portal
.venv/bin/python -m pytest -q                         # 57 tests; clip tests use system ffmpeg
POLL_INTERVAL_S=60 tools/dev_up.sh                   # fake NX :8199 + portal :8099 (SQLite, fresh DB)
.venv/bin/python -m tools.e2e.latency                # push latency + degraded-mode (banner/tone/fallback) checks
tools/dev_up.sh && .venv/bin/python -m tools.e2e.player /tmp   # growing clip, controls, boxes, critical pop-up
POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.arming   # ~3 min: disarm/arm UI, suppression, #24h, timer, schedule
PROBE_PASS_FILE=... .venv/bin/python -m tools.e2e.prod_probe listen 120      # PRODUCTION: SSE over LAN + tunnel at once, per-alarm latency; also arm|disarm|alarms
tools/dev_down.sh
```
- Setup: dependencies install with `~/.local/bin/uv pip install -p .venv -r requirements-dev.txt`, followed by `.venv/bin/playwright install chromium`. The system dependencies for Chromium are already installed.
- `.env.dev` holds dev secrets and `COOKIE_SECURE=false`. The dev login is `admin@twgsecurity.com` / `smoke-test-password-123`.
- `tools/fake_nx.py` imitates NX: login, events, JSON-RPC push, synthetic MPEG-4 clips with the start-time tag, and moving object tracks. Fire events with `curl -X POST localhost:8199/_inject/{panic|line|dock|panic24}`. Each kind has a fixed rule id served at `/rest/v4/events/rules`; panic24's rule is tagged `#24h`.
- To probe real NX from inside the app container (it already holds the site credentials), write a script and run it with
  `docker compose cp x.py app:/tmp/x.py && docker compose exec -T -w /app -e PYTHONPATH=/app app python /tmp/x.py`.
  Keep probes read-only unless the user agrees.
- **Never commit real camera footage.** Tests use synthetic ffmpeg test patterns.
- Commits end with the attribution lines from the session's system reminder. Branch `feature/base-portal`.

## Open items / backlog (roughly in priority order)
1. **Cloudflare Tunnel follow-ups:**
   - put **Cloudflare Access** (Google Workspace SSO) in front, since the portal is on the internet behind a password only
   - audit IPs show the connector's LAN IP: trust it in Caddy and read `CF-Connecting-IP`
   - item 9 below (a real certificate) is moot for the tunnel path
2. **Level rules list**: ordered rules matching analytics subtype, site, camera and caption keywords. Arming now covers the "after hours" case; shared schedules across sites aren't built (schedules are per site).
3. **Warning visibility** (the user said "nothing happened" for a Warning): options offered were a toast on the overview, a louder or longer tone, or flashing the pin amber. No decision yet.
4. **Site 1 over the LAN** (`https://10.1.29.162:7001`) instead of the relay, to avoid the relay 503s. This is only a suggestion.
5. **Site 2 NX account**: grant rule-read rights so `#tags` and `#24h` work there.
6. Zero-downtime deploys. Handle `IntegrityError` on a duplicate `event_key` gracefully first, so two instances could overlap.
7. Security cleanup:
   - replace the classic PAT with the deploy key `~/.ssh/nx_alarm_portal_deploy` (the `Host github-nx-portal` alias is in `~/.ssh/config`; the key isn't added on GitHub yet) or a fine-grained token
   - remove `/etc/sudoers.d/90-twg-claude` when setup is done
   - the user changes the admin password and deletes the temp file
8. A DNS name and a real certificate (drop `tls internal` in the `Caddyfile`).
9. Roadmap from the original plan: multi-tenant admin UI, Google Workspace SSO, a claim/escalation workflow and SOPs per site, reports and CSV export, live video (not just snapshot refresh), Postgres backups, a UI for per-site level overrides.
10. Merge PR #1 once the user is happy.

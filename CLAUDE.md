# TWG Alarm Portal: project brief for Claude

Read this at the start of every session, then **`BACKLOG.md`** (the current build plan). It's the handoff: what exists, how it works, what NX actually
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
  **Cloudflare rewrites `Cache-Control: no-cache` to `max-age=14400`**. Browsers mixed old and new JS after deploys, and the Sites page broke. So pages load assets from `/static/v/<content hash>/…` (`app/static_version.py`: any version is accepted, and responses are cached as immutable). Always reference assets with `{{ asset('js/x.js') }}` in templates.
  Measured through the tunnel: alarms arrive **+3 ms vs LAN**, the SSE stream isn't buffered, and the longest silence is 5.0 s. Password login is the only gate so far (see Open items).
- **The NX API client** (`app/nx/client.py`) is copied from `github.com/TWG-Security/nx-witness-mcp`, with its local changes listed in the file header.
  The claude.ai **NX_Witness MCP** (TWG MCP Gateway) talks to the same systems: `TWG`, `Bethel_Church`, `MedEvac`, `SecTV`, `TheWaterfront`. It's handy for probing NX, and `nx_write_fire_trigger` fires test soft triggers.

## The user
- They run TWG's systems and want progress, not questionnaires. Pick sensible defaults and state them; ask only real blockers.
- When they have to do something (GitHub settings, NX config, sudo), give **numbered, click-level steps**, including exactly where to type commands.
- **Delayed or silently missed alarms are unacceptable. Their words: "NEVER, this is going to be critical".** Every change must keep sub-second delivery and loud failure modes. **Test failure paths end to end and measure the latency** before saying something works. Never claim timing you haven't measured.
- **Deploys: no need to ask (the user said so on 2026-10-02), but always say in the reply that you're deploying, and when it's back.** A restart once landed on their test press. Back up first (`tools/backup.sh`), then check health and "live push connected" for every site.

## Current production state (2026-10-02)
- **Production runs `feature/base-portal` at `125fd84`** (alarm texts via Twilio + `/a/<token>` links, migration 0013). Deployed 2026-10-12 17:57 UTC by the user (backup `20261012-173445Z` first); health OK after 1 s, push connected: sites 2 and 3 after ~13 s, site 1 after ~25 s. Texts are **off** until the user enters Twilio on the Platform page. Before that `ed22d40` (top-down "Cones: on/off" toggle, 15:16 UTC). Before that `bfdb690` (boxes show only Person / Vehicle / Animal via `clips._KINDS`, 14:54 UTC), `b8248f4` (14:50 UTC), `4af200d` (site/alarm click opens top-down, 14:42 UTC), `5809526` (hover live preview, 14:24 UTC), `273dcc6` (alarm details top-down map, 13:49 UTC) and `ac037e8` (top-down view, 13:31 UTC).
- Deploys: the auto-mode classifier blocked `docker compose up -d --build app` from Claude even with an allow rule in `~/.claude/settings.json`; `tools/backup.sh` runs fine. Run the backup, then give the user the `!` command for the restart, then check health and push.
- Before that: `74bee50` (Dismiss button + alarm delivery under bursts; deployed 2026-10-12 12:03 UTC, no migration, health back in 4 s; push connected: site 2 after 1 s, **site 1 after 30 s** (slower than before, likely the relay; polling covered it); backups `20261012-120203Z` before, `20261012-120649Z` after). Before that `2bdee60` (camera editor, 2026-10-02).
  - Tunnel connector `10.0.2.58` is trusted (Platform page / `trust-proxy`); tunnel visitors now show their real IP (verified: 24.245.109.40).
  - Not configured yet: email, Cloudflare, Google sign-in, 2FA requirement, office allowlist.
- **Companies:** only **TWG Security** exists (id 1, `kind=platform`). **No customer companies have been created yet.**
- **Users** (all in TWG):
  - `msupczenski@twgsecurity.com`: admin, the user
  - `mike@twgsecurity.com`: admin, Michael Miller, who asked for verdicts, bulk edit and live beside recorded
  - `e2e-probe@twgsecurity.com`: operator, **deactivated**
  Both admins hold every `platform.*` permission.
- **Arming was verified on production:** a Truss 8 press while site 1 was disarmed arrived in 17 ms and was stored as "disarmed" (alarm 41), with nothing sent to browsers. Both sites are armed, with no schedules.
- **2026-10-12 demo prep:** the user cleared all 961 open alarms (71 site 1, 890 site 2) with the new bulk **Dismiss**, confirmed "it worked"; **0 open** afterwards. A direct bulk SQL ack had been blocked by the permission classifier (Claude can't mass-update production rows without a permission rule). Site 1 was **disarmed**, site 2 armed. Site 2 is noisy (890 alarms in 10 days).
- **Two-step sign-in is in use:** by 2026-10-12 there were 10 recovery codes and 1 passkey in production (a user enrolled). Nobody is *required* yet.
- **Backups:** `tools/backup.sh` runs nightly at **03:15 UTC** (the user's crontab) into `~/backups/nx_alarm_portal/<timestamp>/`, keeping 14. Each run restores the dump into a scratch database to prove it.
  - First verified backup: `20261001-183149Z` (alarms=230, audit_log=510, sites=2, users=3).
  - Log: `~/backups/nx_alarm_portal/backup.log`.
  - **Everything is on this one server**, so an off-server copy (encrypted, since it holds `.env`) is still to do.
- **NX rule health:** `poller.rule_delays` flags enabled alarm-level rules with `action.intervalS > 0` (NX merges repeats inside the interval and writes them when it ends). The map site panel and Sites page show it, along with "can't read rules".
- **The Truss 8 rule's "Interval of action" was 60 s** (repeat presses measured 35 s and 60.6 s late). The user OK'd turning it off, and it has been `intervalS: 0` since 2026-09-30. Verified at 18:00 UTC: two presses 1 s apart arrived 15 ms and 23 ms after their timestamps.
- Sites:
  - **#1 "TWG Security Office"**: Nx Cloud relay `2bc0aef4-cf2f-4f3e-9303-e05c7d1345f8`. Its NX server is also reachable on the LAN at `https://10.1.29.162:7001`.
  - **#2 "Mikey Home Beta"**: relay `c306458c-5f9c-494b-8cb6-d9530ecc0ca8`. It has a CVEDIA plugin.
- A temporary operator `e2e-probe@twgsecurity.com` exists but is **deactivated**. Reactivate it with a new password for `tools/e2e/prod_probe.py`, then deactivate it again.
- Admin login: `msupczenski@twgsecurity.com`. The temporary password is in `~/portal-admin-temp-password.txt` (mode 600). The user should change it and delete the file.
- The user **turned off the camera ONVIF analytics rules** on site 1; only soft triggers remain there.
  The Truss 8 soft trigger rule (id `79239a08-fb91-4b89-a6ca-6562ee72639f`, trigger `256dacab-d698-4a32-b9ec-135ba73ba127`,
  device `3c78795b-9824-22c1-88ad-cdb477f27197`) is tagged **`#alarm`** (the user changed it from `#warning`). Its action is "Write to Log", with "Interval of action" off. It's the one to fire for tests; it chimes as an Alarm in the portal.
- Measured on production:
  - soft trigger → portal: **87–219 ms**, by push
  - critical siren: was up to 0.5 s after the pop-up (sound loop tick); fixed 2026-10-02 to play on arrival (33-42 ms in dev)
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
| Models: tenants, users, groups, sites, alarms, alarm_notes, audit_log | `app/models.py` (every table has `tenant_id`); migrations `migrations/versions/0001-0012` |
| Backups | `tools/backup.sh` (pg_dump + .env + checksums, restore-verified, rotation) |
| Alarm levels (critical 1 / alarm 2 / warning 3 / ignore) | `app/services/alarm_filter.py` |
| NX row → portal fields, captions | `app/services/nx_events.py` |
| Pollers, ingest, rule `#tags`, rule health (`rule_delays`), site status, "site connection lost" alarm | `app/services/poller.py` |
| NX push (JSON-RPC websocket) | `app/services/push.py` |
| Acknowledge + NX write-back (forced-ack clear or bookmark) | `app/services/ack.py` |
| Follow-up notes (append-only) | `alarm_notes` (migration 0006), `GET/POST /api/alarms/{id}/notes`, bus event `alarm.note`; drawer "Notes"; in PDF/ZIP |
| Sign-in protection (bans, account lock, allowlist) | `app/security_guard.py`; client IP `app/net.py`; settings cache `app/platform_settings.py`; `BanGate` in `app/main.py`; tables `platform_settings`, `auth_events`, `ip_bans`, `ip_allowlist` (migration 0008) |
| Cloudflare edge bans | `app/cloudflare_edge.py` (IP Access Rules, notes prefix `twg-alarm-portal auto-ban`); sweep every 60 s in `security_guard.run_sweeper` |
| Platform page (TWG only) | `app/routers/platform.py`, `templates/platform.html`, `js/platform.js`; tests `tests/test_sign_in_protection.py`, e2e `tools/e2e/signin.py` |
| Two-step sign-in (TOTP, recovery codes, passkeys) | `app/mfa.py`; login stages in `app/routers/auth.py` (`pending` in the session between stages); templates `login*.html`, `recovery_codes.html`; `js/login.js`, `js/webauthn.js`; tests `tests/test_two_factor.py` (+ `tests/soft_passkey.py`, a software authenticator), e2e `tools/e2e/twofactor.py` (Chrome virtual authenticator) |
| Sessions (one row per browser) | `app/sessions.py` (`check` in `deps.current_user`, adopts old cookies), `user_sessions` (migration 0009); revoked streams get `event: signedout` → `common.js signedOut()` |
| Account page, admin user security, company 2FA switch | `app/routers/account.py`, `templates/account.html`, `js/account.js`; password rules `app/passwords.py` |
| Email (SMTP, branded template, send log) | `app/mail.py` (`send_now` / `send_later`, 3 attempts), `templates/email/layout.html`, `email_log` (migration 0010) |
| Invites, setup and reset links | `app/invites.py`, `app/tokens.py` (`user_tokens`: SHA-256 only, setup 7 d, reset 30 min); pages `/setup`, `/forgot`, `/reset`, `/login/password-expired` in `app/routers/auth.py`; `password_form.html`, `forgot.html`; tests `tests/test_email_invites.py` (aiosmtpd sink), e2e `tools/e2e/email.py` |
| Alarm texts (Twilio) + no-sign-in alarm links | `app/sms.py` (`notify` from `poller._announce`: never waits; background tasks share 2 DB slots and 4 Twilio slots; per-person flood limit → summary text), `app/alarm_links.py` (`/a/<token>`, 18 random bytes, SHA-256 stored), `app/routers/links.py` (`/a/{t}`, `/api/a/{t}/clip|objects`, `/media/a/{t}/snapshot.jpg|live.jpg|live.webm|clips/…`), page `alarm_link.html` + `js/alarm_link.js` (reuses `player.js` with `apiBase`/`mediaBase`); `users.phone`/`sms_level`, `platform_settings.twilio_*`/`sms_*`, `sms_log`, `alarm_links` (migration 0013); Platform tab "Text messages" (field ids `tx-*`: **`s-*` belongs to the Sessions tab**), Users page "Alarm texts" column; `TWILIO_API_BASE` (dev points it at `tools/fake_nx.py`'s fake Twilio: `/_twilio`, `/_twilio/mode/{ok,fail,hang}`); tests `tests/test_sms.py`, e2e `tools/e2e/sms.py` |
| Google sign-in (OIDC + PKCE) | `app/routers/sso.py` (`/auth/google`, `/auth/google/callback`), settings on `platform_settings.google_*` (migration 0011); tests `tests/test_google_sso.py` |
| Companies / tenancy | `app/scope.py` (`Scope`, `get_scope`), `app/routers/tenants.py` (`/api/scope`, `/api/tenants`, logo, `/branding/{id}/logo`), `templates/companies.html` + `js/companies.js`; tests `tests/test_tenancy.py`, e2e `tools/e2e/tenancy.py` |
| Verdicts (real / false / dismissed), bulk edit | `ack.py` (`acknowledge(verdict)`, `set_verdict`; `dismissed` skips the NX bookmark but still clears forced-ack events), `POST /api/alarms/bulk` (needs `alarms.bulk_edit`, ≤500 ids; `alarms.js` chunks); e2e `tools/e2e/dismiss.py` |
| Groups & permissions | `app/permissions.py` (catalog, `require()`), `user_groups` / `user_group_members` (migration 0005), `/api/groups`; `deps.current_user` loads `user._perms`, pages get `CFG.perms` |
| Live video | `GET /media/alarms/{id}/live.webm`: relays NX `/media/<dev>.webm?resolution=640x360` (≤6 per site, 20 min cap, commits the DB session first so no connection is pinned); `static/js/live.js` |
| Incident report PDF / evidence ZIP | `app/services/report.py` (reportlab + Pillow; CPU work in `asyncio.to_thread`); `GET /api/alarms/{id}/export?format=pdf|zip&note&pre&post&quality` |
| Site arming: state maths, schedules, `#24h`, announce loop | `app/services/arming.py` (migration 0004); API `POST /api/sites/{id}/arm|disarm` |
| Clips (growing → full), ffmpeg, cache, prefetch, bounding boxes | `app/services/clips.py` |
| SSE stream, snapshot/clip/objects endpoints | `app/routers/stream.py` |
| JSON API (sites, alarms, audit, users, settings, geocode) | `app/routers/api.py` |
| Tile proxy + disk cache: OSM street map, USGS aerial imagery (`/tiles/sat/`, NAIP 0.3 m above z16) | `app/routers/tiles.py` |
| 3D site view (CesiumJS 1.146 vendored in `static/vendor/cesium`, lazy-loaded) | `static/js/globe.js`, opened from `map.js` (site panel "3D view"); e2e `tools/e2e/globe.py` |
| Top-down site view (Leaflet, aerial, camera cones + editor) | `static/js/plan.js`, opened from `map.js` (site panel "Top-down view"); styles "top-down view" in `theme.css`; e2e `tools/e2e/topdown.py` |
| Frontend (no build step) | `app/templates/*.html`, `app/static/js/*.js`, `app/static/css/theme.css`, Leaflet vendored in `app/static/vendor/leaflet` |

JS modules:
- `common.js`: API, the open-alarm store, the SSE connection with reconnect, watchdog and fallback polling. Acks/updates from the stream are applied to the store at once but redrawn at most every 100 ms (`queueChange`); `alarm.new` flushes first and is never batched. Store listeners get `{alarm}` or `{alarms}`.
- `console.js`: entry point on every page
- `sound.js`: siren/chime/tones, silence, mute, one "leader" tab plays
- `critical.js`: the pop-up
- `drawer.js`: alarm details
- `player.js`: clip player, timeline and boxes
- `map.js`: overview with sites, map and live feed (draws the top `FEED_MAX` = 100 cards, then "+N more")
- `live.js`: live WebM view (drawer side pane, critical pop-up's Live button)
- `globe.js`: the 3D site view (Cesium), imported by `map.js` only when "3D view" is clicked; software-WebGL refusal and frame watchdog
- `plan.js`: the top-down view: turns the existing Leaflet map into aerial + camera cones; same editor API as `globe.js` (`editCamera`, `stopEditing`, `editing`, `currentEditor`), so `map.js` drives either through `view()`
- `companies.js`: TWG's Companies page
- `alarms.js` (incl. bulk edit), `sites.js`, `site_form.js` (arming schedule), `settings.js` (incl. company branding), `audit.js`, `users.js` (incl. groups)

### Multiple companies (tenants)
- **Platform company:** exactly one tenant has `kind="platform"`: TWG Security (migration 0007 set it to the first tenant). Every other company is `kind="customer"`.
- **Same address for everyone:** the login decides the company. The user chose this over per-company subdomains.
- **Customers** see only their own data. TWG staff with **`platform.view`** pick a view from the top-bar switcher (`POST /api/scope`, stored in the session):
  - their own company (the default)
  - **All companies**: read-only overview; per-company pages (settings, users, groups) and creates need a single company
  - **one company** (support mode)
- **Writing into another company** needs **`platform.support`** and is audit-logged in **that company's** log under the TWG user's name, shown there with a "TWG support" chip. Opening a company logs `support.viewed`.
- **`platform.manage`** creates companies (with their first admin), sets branding, and turns sign-in on or off.
- **Platform permissions are TWG-only:** `platform.*` only exist in the platform tenant. A customer's admins don't get them and its groups can't grant them (`permissions.applicable`).
- **The isolation rule for all new code:** every query filters with `Scope.where(Model.tenant_id)` (`app/scope.py`), and every write calls `scope.require_write(obj.tenant_id)` or `scope.write_tenant()`. Never use `user.tenant_id` for data access. `tests/test_tenancy.py` hits every endpoint across companies and expects 404; **add new endpoints to it**.
- **Alerting (the user's decision):** other companies' alarms are shown but **never sound or pop up**. The open-alarm store (`/api/alarms?alerting=1`) and the SSE subscription always include the user's own company, so **TWG's own alarms keep sounding while TWG views another company**. In the browser this is `isMine` (sound, critical pop-up) versus `inView` (display) in `common.js`.
- **Disabling a company** blocks its sign-in and ends open sessions, but **its sites keep being monitored** (pollers are untouched).
- **Branding:** `tenants.display_name` and `logo` (uploads re-encoded to PNG ≤600×160, never served as uploaded). They're used in the top bar ("Powered by TWG Security" for customers) and on incident PDFs. A customer's own admin edits them under Settings → Company branding.
- `python -m app.cli create-tenant --name … --admin-email …` creates a company from the shell.

### Sign-in protection (the client address and bans)
- **Client address:** uvicorn trusts `X-Forwarded-For` only from the docker network (Caddy). `app/net.py` then believes `CF-Connecting-IP` only when the peer is a trusted proxy (loopback, `TRUSTED_PROXY_IPS`, or the Platform page; private addresses only). Use `deps.client_ip` everywhere.
- **Bans:** `security_guard.record()` stores every attempt in `auth_events` and bans an IP past the threshold (escalating, then permanent). `BanGate` answers banned IPs on the sign-in pages and on session-less `/api/` calls only; **a ban never cuts off a signed-in session**.
- **Never banned:** internal ranges (`net.INTERNAL`), trusted proxies, the allowlist, and IPs with a signed-in request in the last 15 min (in memory, `note_session_ip` in `deps.current_user`).
- **Account lock** (per email, any IP) fails open on DB errors. Admin unban/unlock writes an `auth_events` row with outcome `cleared`; only later failures count.
- **Cloudflare** is best-effort and background (`after_commit` → `spawn`); the sweeper pushes missing rules and removes expired ones. Removal runs whenever a token exists, even with the switch off.
- Settings live in `platform_settings` (id 1), read through the `platform_settings.current()` snapshot (refreshed on save, at most 30 s old). Secrets are Fernet-encrypted, never returned, and the audit log records field names only.

### Two-step sign-in and sessions
- Required for: TWG users if `mfa_require_twg`; customers if `mfa_customers == "all"`, else `tenant.settings["require_2fa"]` (`mfa.required_for`).
- After the password: TOTP on (or required + passkeys) → `/login/2fa`; required but nothing set up → `/login/enroll`; else signed in. A passkey sign-in skips all of it.
- Passkeys: RP ID `WEBAUTHN_RP_ID` (default `alarmportal.twgsecurity.net`), origins `WEBAUTHN_ORIGINS`. The page hides passkey buttons on other hosts (the LAN IP). Dev uses `localhost` (set in `tools/dev_up.sh`).
- **A revoked or expired session must never look live:** the page keeps showing, with the SIGNED OUT banner and tone. Keep `signedOut()` in `common.js` as the only reaction to a 401.
- Session limits (Platform page): `session_closed_h` (12) counts from the last request, so an open screen never expires; `session_max_h` and `idle_timeout_min` default to 0 (off).

### Passwords, invites, email
- **Every place a password is set calls `passwords.check()`** (rules on the Platform page): user create/reset, company create, setup, reset, Account page, expired change, CLI. Keep it that way for new paths.
- Invited users have `password_hash = ""` (`User.is_invited`), so a password sign-in can't succeed until they use the setup link.
- Emails never block a request and never decide anything: with email off or failing, the admin still gets the link. The forgot-password page answers the same for any email.

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
- **Burst safety (measured 2026-10-12, dev, 500-600 alarm bursts):** never hold a DB session across slow work (clip builds, NX calls): the pool is 5+10 and a burst of loud alarms once took all of it, stalling ingest 30 s+. ffmpeg/ffprobe run under `nice -n 19` (`clips._run`): at normal priority they starved the app and alarms reached browsers 2-3 s late. After both fixes: critical pop-up 92-178 ms with 500 open alarms and 4 ffmpeg running; 232 ms during a 503-alarm bulk dismiss.

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

**Live video** (measured through the relay on site 1, 2026-10-01)
- `GET /media/<device>.webm?resolution=640x360`: VP8, about **100 KB/s**, first byte in 0.25 s.
- `.mpjpeg`: about 26 fps but **1.7 MB/s**. Not used.
- Through the portal on production (2026-10-01), alarm 59: LAN first bytes 0.17 s at 104 KB/s; Cloudflare tunnel 0.21 s at 102 KB/s.

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
.venv/bin/python -m pytest -q                         # 169 tests; clip and report tests use system ffmpeg
POLL_INTERVAL_S=60 tools/dev_up.sh                   # fake NX :8199 + portal :8099 (SQLite, fresh DB)
.venv/bin/python -m tools.e2e.latency                # push latency + degraded-mode (banner/tone/fallback) checks
tools/dev_up.sh && .venv/bin/python -m tools.e2e.player /tmp   # growing clip, controls, boxes, critical pop-up
POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.arming   # ~3 min: disarm/arm UI, suppression, #24h, timer, schedule
tools/dev_up.sh && .venv/bin/python -m tools.e2e.export /tmp      # export ZIP from the drawer, checksums, alarm latency mid-export
tools/dev_up.sh && .venv/bin/python -m tools.e2e.verdicts /tmp    # live beside recorded, verdict buttons, groups, operator bulk edit
tools/dev_up.sh && .venv/bin/python -m tools.e2e.signin /tmp      # 5 bad sign-ins -> blocked page, operator's alarms unaffected, unblock on Platform page
tools/dev_up.sh && .venv/bin/python -m tools.e2e.twofactor /tmp   # authenticator app, passkey (virtual authenticator), forced enrolment, remote sign-out -> SIGNED OUT banner + tone
tools/dev_up.sh && .venv/bin/python -m tools.e2e.email /tmp       # SMTP sink on :8125: test email, invite -> setup link, forgot -> reset link
tools/dev_up.sh && .venv/bin/python -m tools.e2e.tenancy /tmp     # two companies (2nd fake NX on :8198): isolation, branding, views, who hears what
POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.cameras /tmp # place cameras on the 3D view, live window, alarm turns a camera red
POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.topdown /tmp # same on the top-down view, + alarm latency while open / panning, exit restores the street map
POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.dismiss /tmp # Dismiss everywhere; 600-alarm burst, bulk dismiss, critical latency meanwhile
POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.sms /tmp     # alarm texts: Platform tab, Users numbers, link on a phone (clip, live), Twilio failing/hanging + alarm latency
POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.globe /tmp   # 3D view: software-GL refusal, forced open + watchdog, alarm latency while open, failed load
PROBE_PASS_FILE=... .venv/bin/python -m tools.e2e.prod_probe listen 120      # PRODUCTION: SSE over LAN + tunnel at once, per-alarm latency; also arm|disarm|alarms
tools/dev_down.sh
```
- Setup: dependencies install with `~/.local/bin/uv pip install -p .venv -r requirements-dev.txt` (Phase 2-3 added pyotp, qrcode, webauthn; aiosmtpd for tests), followed by `.venv/bin/playwright install chromium`. The system dependencies for Chromium are already installed.
- `.env.dev` holds dev secrets and `COOKIE_SECURE=false`. The dev login is `admin@twgsecurity.com` / `Smoke-test-password-123!`.
- `tools/fake_nx.py` imitates NX: login, events, JSON-RPC push, synthetic MPEG-4 clips with the start-time tag, and moving object tracks. Fire events with `curl -X POST localhost:8199/_inject/{panic|line|dock|panic24}`. Each kind has a fixed rule id served at `/rest/v4/events/rules`; panic24's rule is tagged `#24h`.
- To probe real NX from inside the app container (it already holds the site credentials), write a script and run it with
  `docker compose cp x.py app:/tmp/x.py && docker compose exec -T -w /app -e PYTHONPATH=/app app python /tmp/x.py`.
  Keep probes read-only unless the user agrees.
- **Camera e2e tests serve stand-in aerial tiles** (`common.stub_aerial_tiles`): on 2026-10-12 USGS NAIP took 30 s+ per uncached tile, and on dev's HTTP/1.1 those requests held all 6 browser connections, so API calls (camera list, saves) queued behind them. Production is HTTP/2 via Caddy and pre-loads z17-19 around each site (281 z19 tiles cached on 2026-10-12).
- **Never commit real camera footage.** Tests use synthetic ffmpeg test patterns.
- Commits end with the attribution lines from the session's system reminder. Branch `feature/base-portal`.

## Open items / backlog (roughly in priority order)
14d. **Alarm texts (Twilio)**, asked for 2026-10-12: "SMS when an alarm comes in, with a link to the alarm, connect without auth, unique id in the URL, Twilio"; then "a platform setting, and phone numbers in the user list". Built 2026-10-12 (see the Architecture row). Defaults chosen: link valid 24 h, 10 texts per person per 10 min then one summary, a newly added number texts Critical + Alarm, links are view-only (details, clip, live; acknowledging needs sign-in), only the alarm's own company is texted. Measured dev: alarm on screen 29-77 ms with Twilio OK / rejecting / hanging; Twilio accepted the text 0.1 s after the alarm reached the portal.
   - **Waiting on the user:** Twilio Account SID, Auth Token and a number with SMS, **plus US carrier registration** (toll-free verification, or A2P 10DLC for a local number; days to approve; unregistered numbers get error 30034). Trial accounts only text verified numbers.
   - If Cloudflare Access goes in front (item 1), add a **Bypass** policy for `/a/*`, `/api/a/*`, `/media/a/*` and `/static/*`, or the links stop working on phones.
   - **Consent (the user's ask, 2026-10-12, migration 0014):** `users.sms_consent` none → pending (opt-in text sent when a number + level is saved) → yes (replied YES) / stopped (STOP or Twilio 21610). **Only `yes` gets alarm texts.** Replies: `POST /twilio/sms` in `routers/links.py`, validated with `sms.valid_signature` against `portal_url + /twilio/sms` (and the request URL). The user must set that webhook on the Twilio number (Platform tab shows the URL and warns until a reply arrives). Public terms `/sms-terms` (`sms_terms.html`, wording is the standard CTIA template; the user should have it reviewed). Help contact: `platform_settings.sms_help_contact`.
   - **Text layout** (user, 2026-10-12): one item per line: alarm / site / address / camera + time / `Directions: https://www.google.com/maps/dir/?api=1&destination=lat,lng` / View alarm / Reply STOP. ~290 chars (2 SMS segments; 1 MMS).
   - **Picture (MMS)** (user, 2026-10-12): `sms.alarm_picture` = `stream.snapshot_bytes` + `clips.objects` + `report._annotate`, cached per alarm, ≤4 s (`PICTURE_BUDGET_S`) or the text goes without; Twilio fetches `/media/a/<token>/alarm.jpg`; a 4xx with a picture is retried without it (`NUMBER_ERRORS` excepted). Switch `platform_settings.sms_pictures`.
   - **Background tasks** (user: "make sure they're not getting stuck"): `sms._spawn` wraps every task in `wait_for(TASK_MAX_S=120)` (summaries: window + 120 s), `sms.shutdown()` in `main.lifespan` (2 s grace then cancel), `sms.status()` on the Platform tab. A test hang on 2026-10-12 was a test cancelling a text mid-DB-write after the SQLite engine closed (aiosqlite's shielded close never finished); tests now `drain()` instead.
   - Possible next steps: acknowledge from the link; texting TWG on-call for customer companies' alarms; Twilio delivery-status callbacks.
14c. **Site 1 "Repeat alarms delayed" (read 2026-10-12, the user said "not now"):** four enabled rules with Interval of action 60 s and no interval-0 rule for the same event: `32fac496` AXIS front-of-office (analytics `nx.axis.{c95466c7-…}`, cam `71241cb2`, desktopNotification), `44bfffa4` ObjectClass.Human (cam `1542c3fe`, push), `e0974f76` cvedia armed_person (cam `68512ea9`, **writeToLog**, caption filter "smoking" looks like a leftover), `bb23e5b2` Nx Training soft trigger (cam `2df009a1`, speak). Fix offered: untick the interval on `e0974f76`; add Write-to-log copies (interval off) of the other three. Needs the user's OK (writes to NX). The TWG gateway's NX connector withholds the rule list (2 password fields); read rules through the app container instead.
14b. **Top-down site view** (the user asked 2026-10-12 for an NX-style flat map "where we can add cameras, not in 3d mode", with a screenshot of NX's map: drone photo, blue cones). Deployed 2026-10-12 13:31 UTC (`ac037e8`).
   - Map page → site panel → **Top-down view** (now the primary button; 3D view stays). `plan.js` swaps the map's tile layer for aerial (`/tiles/sat`, native z19, overzoom to z21), adds cones (pane `planCones`) and camera dots, and reuses the Place cameras panel and editor. Same `camera_places` as 3D. Exit restores the street map. Opening one view closes the other.
   - Alarm safety (dev, measured): alarms on screen 16-92 ms with the view open + live video; 44-61 ms while panning. No WebGL, so no watchdog needed.
   - Alarm details (`drawer.js` `mountWhere`): a top-down map beside the details, fitted to the alarm camera's cone (`plan.focusCamera`), red + flashing (`.plan-cone--alarm`). Opening an alarm with the Map page's top-down open jumps it to the camera.
   - Hover preview (`map.js` `hoverCamera`/`showHover`): 250 ms delay, `mountLive(..., { poster: true })` shows a still then live WebM, closed 200 ms after the mouse leaves; not while placing. Measured dev: picture ~0.3 s, video ~0.4 s, alarm 70-112 ms meanwhile.
   - **Site and alarm clicks go straight to top-down** (the user insisted, 2026-10-12): `map.js` `select()` opens `openPlanView` (3D open: flies there instead); `focusAlarm` opens it at the camera; `deselect()` (✕, Show all sites) and Exit top-down return to the overview. e2e login helper stubs aerial tiles for every page.
   - **Don't add a class named `alarm` to anything outside the alarm list**: `.alarm` is the Alarms-page row style (grid). It broke the live window; use `cam-alarm`, `plan-cam--alarm` etc.
   - **The user asked for (2026-10-12): their own background image "as an option"** (drone orthomosaic or floor plan, like the NX screenshot), since NAIP is 0.3 m/px and blurry past z19. Design idea: upload per site, pin it to the map by its corners (or GeoTIFF bounds), keep cameras in lat/lng so both views keep working.
0. **Account security is DEPLOYED** (2026-10-02 00:23 UTC, `857d825`, migrations 0008-0011; see "Current production state"). Left to do:
   - allowlist the office: `docker compose exec -T app python -m app.cli allow-ip --ip 204.186.88.58 --label "TWG office"` once the user confirms that's the office's public IP (it's this server's)
   - still open from the user: SNMP (q1 in BACKLOG.md), 2FA scope (q2; today nobody is required), SMTP mailbox (q3), Cloudflare token + Zone ID (q4), Google OAuth client (q7). All are entered on the Platform page.
1. **Cloudflare Tunnel follow-ups:**
   - put **Cloudflare Access** in front, since the portal is on the internet behind a password only. Click-level steps (one-time PIN, policy, 1-week session) were given to the user on 2026-09-30; they haven't confirmed it's done.
     **A policy limited to `@twgsecurity.com` would lock out customer companies**: add their domains, or allow one-time PIN for any email.
   - optional: the user can set Cloudflare *Browser Cache TTL* to "Respect Existing Headers" (the portal no longer depends on it)
2. **First customer company**: nothing to build. Check with the user when they onboard one (Companies → New company); also check Cloudflare Access (item 1).
3. **Off-server backup copy**: encrypted, e.g. to Google Drive via the TWG gateway, or another host.
4. ~~Flaky export e2e~~: found on 2026-10-01: it read the audit page while it still said "Loading…". Fixed (also in `tenancy`).
5. **Level rules list**: ordered rules matching analytics subtype, site, camera and caption keywords. Arming now covers the "after hours" case; shared schedules across sites aren't built (schedules are per site).
6. **Warning visibility** (the user said "nothing happened" for a Warning): options offered were a toast on the overview, a louder or longer tone, or flashing the pin amber. No decision yet.
7. **Site 1 over the LAN** (`https://10.1.29.162:7001`) instead of the relay, to avoid the relay 503s. This is only a suggestion.
8. **Site 2 NX account**: grant rule-read rights so `#tags` and `#24h` work there.
9. Zero-downtime deploys. Handle `IntegrityError` on a duplicate `event_key` gracefully first, so two instances could overlap.
10. Security cleanup:
   - replace the classic PAT with the deploy key `~/.ssh/nx_alarm_portal_deploy` (the `Host github-nx-portal` alias is in `~/.ssh/config`; the key isn't added on GitHub yet) or a fine-grained token
   - remove `/etc/sudoers.d/90-twg-claude` when setup is done
   - the user changes the admin password and deletes the temp file
11. A real certificate for the LAN address (drop `tls internal` in the `Caddyfile`); the tunnel path already has one.
12. Roadmap: Google Workspace SSO (or via Cloudflare Access), per-company subdomains (declined for now in favour of one address), a claim/escalation workflow and SOPs per site, reports and CSV export, a UI for per-site level overrides (`sites.alarm_types`), and tidying the unused imports in `app/routers/stream.py`.
13. Merge PR #1 once the user is happy. **Push first:** 10+ commits were unpushed as of 2026-10-12.
15. **Large backlogs** (follow-ups to 2026-10-12): `loadOpenAlarms` fetches at most 500, so with more open the store and badge undercount; the Alarms page loads 100 per "Load more", so a big bulk dismiss needs many clicks (a "Dismiss all matching" server-side action would fix it). Exports still hold a DB connection while waiting up to 90 s for the clip.
14. **3D site view ("God's Eye View")**: v1 deployed 2026-10-02 (`85b7265`). The user then said the flat free imagery looked bad ("crap 3d map") and asked for the look of gods-eye-view, "where I can add the cameras in". v2 deployed 17:53 UTC (`2aa398e`): HUD, cameras, Google 3D buildings support (waiting on a key).
   - Where: map page → site panel → **3D view** replaces the map pane (feed and site list stay). `static/js/globe.js` (Cesium), wired in `map.js`; HUD markup in `map.html`, styles under "3D site view" in `theme.css`.
   - **Camera editor** (user feedback 2026-10-02: fixed distance and preset widths were bad): `globe.editCamera` places by click-spot then aim-with-mouse (direction and distance follow it), then drag handles (white centre = move, orange tip = aim + distance, yellow sides = any width up to a 360° dome); the panel shows Aim/Width/Distance number boxes and saves every finished change. Clicking a camera while Place cameras is open edits it. Limits: width 5-360°, distance 2-500 m.
   - **Cameras:** `sites.camera_places` JSON (migration 0012), `{device id: {name, lat, lng, heading, fov, range_m}}`. API: `GET /api/sites/{id}/cameras` (NX list + placements), `PUT|DELETE /api/sites/{id}/cameras/{device}` (admins). Live: `/media/sites/{id}/cameras/{device}/live.webm|jpg`, placed cameras only, sharing `_live_webm` and the 6-per-site cap with alarm live view. Tests `tests/test_camera_places.py`, tenancy rows added; e2e `tools/e2e/cameras.py`.
   - **3D buildings need `GOOGLE_MAP_TILES_KEY`** in `.env` (Map Tiles API browser key restricted to `https://alarmportal.twgsecurity.net/*` and `https://10.1.10.97/*`; click-level steps were given to the user 2026-10-02). Not tested yet: no key. With it, `createGooglePhotorealistic3DTileset` replaces the globe and the flight re-aims at the sampled ground height. The map page sends `Referrer-Policy: strict-origin-when-cross-origin` so the key's website restriction sees our origin.
   - Imagery without a key: USGS The National Map, public domain. `tiles.run_prefetcher` (started in `main.lifespan`) pre-loads z17-19 within 250 m of every site, 2 at a time, 30 s after start and daily. z<=16 tiled `USGSImageryOnly`; z17-19 `USGSNAIPPlus/ImageServer/exportImage` per tile (0.3 m, 1-8 s first time, cached 30 days). Cesium ion is never used.
   - **Alarm safety (measured, headless SwiftShader = worst case):** alarms 44-85 ms with the view closed; 300-800 ms late during load and frames of 3.3 s while dragging on software GL. So: software WebGL is refused; a frame watchdog closes the view (one frame ≥1 s or 3 ≥150 ms within 10 s; tripped within ~2 s on SwiftShader); a failed load leaves the 2D map. With the watchdog off (test switch), an alarm reached the screen in 103-114 ms with the 3D view open and a camera's live video playing. Test switch: localStorage `twg-3d-force` = 1 (skip graphics check) or 2 (also no watchdog). **Not measured on a real GPU / the wall-screen PC yet.**
   - Next, if wanted: tune for Google tiles once there's a key (watchdog limits, `maximumScreenSpaceError`), a slow orbit for the wall screen, remember 3D mode across reloads, camera height/tilt. Voice control (gods-eye-view uses OpenAI) is not planned.

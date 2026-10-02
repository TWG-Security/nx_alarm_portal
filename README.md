# TWG Alarm Portal

One place where TWG Security operators see alarms from every NX Witness deployment.

- **Map** (OpenStreetMap, no API key): every site is a pin, and the pin changes when the site needs attention:
  - **red, pulsing**: open security alarm
  - **amber**: open system-health issue only
  - **green**: all clear
  - **grey**: site offline or login failing
- **Live overview**: sites on the left, the map in the middle, and on the right a live alarm feed. Each card shows the level, site and address, camera, and a running "active for" timer.
- **Alarm levels, configurable per NX event type** (Settings page):

  | Level | Behavior |
  |---|---|
  | **Critical** | Pop-up on every page, siren repeating every 4 s until acknowledged or silenced |
  | **Alarm** | Red card in the feed, chime repeating every 30 s |
  | **Warning** | Amber card in the feed, one soft tone |
  | **Ignore** | Not stored or shown |

  **Per NX rule:** type `#critical`, `#alarm`, `#warning` or `#ignore` in the rule's **Title or Comment** in NX. The tag overrides everything else, and the portal picks up rule edits within a minute.
  Levels are chosen in this order: rule tag → "Force acknowledgement" → per-site override → Settings → built-in default. The alarm details show which one applied.

  **Silence 2 min** pauses the repeats (a new critical breaks the silence). With several portal tabs open, only one plays sound.
- **Unified alarm queue**: filter by site, level and state.
- **Alarm video**:
  - Every alarm opens with a looping clip from 10 s before to 20 s after, in the details drawer and in the critical pop-up.
  - The timeline shows an alarm marker and marks where analytics objects were present.
  - Controls: play/pause, 0.1 s frame steps (← →), 0.25×–4× speed, jump to the alarm, widening the window 15 s earlier or later (up to 5 min), HD and download.
  - **Live video beside the recorded clip** in the alarm drawer (NX's WebM stream relayed by the portal, ~0.8 Mbit/s, first frame in about 1 s). It falls back to stills if the stream fails.
  - **Analytics bounding boxes** are drawn over the video in sync, labeled with the object type and its first attribute. The event's own object is red; other objects in the clip are orange.
  - Clips for Critical and Alarm events are pre-built as soon as the footage exists, so they open instantly.
- **Acknowledge as Real event or False alarm**, with a disposition note.
  - The portal writes the ack back to NX, either clearing a forced-acknowledgement notification or adding a bookmark.
  - **Follow-up notes** can be added to any alarm afterwards. They're append-only and shown live to other operators.
  - Everything goes into an append-only audit log.
- **Bulk edit (admin override)**: mark many alarms real or false at once. Admins can do this, and so can any user group granted it on the Users page (**groups & permissions**).
- **Incident export**: a TWG-branded **PDF report** (summary, site-time and UTC timestamps, screenshots with analytics boxes, timeline, operator notes, clip checksum). An **evidence package (ZIP)** adds the MP4 clip, stills and a SHA-256 manifest.
- **Arm / disarm per site**, by hand (with an optional auto re-arm) or on a **weekly schedule** in the site's time zone.
  - While a site is disarmed, security events are recorded but not raised. System alarms and NX rules tagged `#24h` still raise.
  - The armed state is computed when each alarm arrives, so a scheduled arm can't be missed.
- **NX rule health**: flags NX rules whose "Interval of action" delays repeat alarms, and sites whose portal account can't read rules.
- **Add sites through the vmsproxy relay**: enter the site's Nx Cloud ID, the credentials and a map pin, then click **Connect**.
- Dark mode is the default; a light theme is available.
- **Multiple companies**: other security companies get their own portal at the same address, with their own name and logo ("Powered by TWG Security"). They see only their own data.
  - TWG staff switch between their own company, **All companies** (overview) and one company (**support mode**, logged in that company's audit trail).
  - Other companies' alarms never sound on TWG's screens; TWG's own always do.
  - The **Companies** page creates companies and their first admin, shows each one's health, and turns sign-in on or off.

## Account security

- **Sign-in protection:** every sign-in attempt is recorded.
  - 5 failures from one address in 10 minutes block it for 15 minutes. Each later block lasts 4× longer (capped at 7 days), and the 5th is permanent.
  - 10 failures for one email from any addresses lock that account until the window passes.
  - **Never blocked:** the office network (so `https://10.1.10.97` always works), the tunnel connector, the allowlist, and any address a signed-in user was active from in the last 15 minutes. A block only stops the sign-in page; it never signs anyone out or touches the live alarm feed.
  - With a Cloudflare API token, each block is also pushed to Cloudflare so the attacker stops at the edge. That push is best-effort: the portal's own block holds even if Cloudflare is down.
- **Two-step sign-in:** an authenticator app (with 10 single-use recovery codes) or **passkeys** (fingerprint, face, device PIN; they work at `https://alarmportal.twgsecurity.net`, not on the IP address).
  - "Sign in with a passkey" needs no password.
  - It can be required for TWG and for every company, or each company decides. Anyone required to use it is walked through set-up at their next sign-in.
- **Sessions:** every signed-in browser is listed on your Account page, where you can sign it out, or sign out everywhere. Admins can do the same for their users.
  - **A screen that's signed out is never quiet:** it shows a red **SIGNED OUT, not receiving alarms** banner and sounds the tone every 10 s until someone signs in again.
- **Invites and password resets by email:** new users get a TWG-branded link (valid 7 days, single use) to choose their own password. "Forgot password?" emails a 30-minute link. With email off, the admin gets the link to pass on.
- **Password rules:** 12+ characters with upper and lower case, a number and a symbol (editable), enforced everywhere a password is set. Optional expiry.
- **Sign in with Google**, for existing accounts only (matched by the Google-verified email).

### Where the settings are
- **Platform** (top menu; TWG staff only, changes need *Manage companies*). Tabs:
  - **Sign-in protection:** ban rules, allowlist, blocked addresses, locked accounts, recent sign-in attempts, trusted tunnel connector
  - **Cloudflare**
  - **Two-step sign-in**
  - **Sessions**
  - **Passwords**
  - **Email** (SMTP, test email, recent emails)
  - **Google sign-in**
  Each tab with outside setup has click-level steps on the page.
- **Your name** (top right) opens your **Account**: password, authenticator app, passkeys, where you're signed in.
- **Settings → Sign-in security:** a company admin can require two-step sign-in for their company, unless TWG sets it for everyone.
- **Users:** invite by email, two-step status, and per user **Sign-in security** (reset two-step, sign them out).

### Break-glass commands (on the server)
```bash
docker compose exec -T app python -m app.cli unban --ip 203.0.113.7          # lift a sign-in block (and its Cloudflare rule)
docker compose exec -T app python -m app.cli allow-ip --ip 198.51.100.10 --label "TWG office"
docker compose exec -T app python -m app.cli clear-lock --email someone@example.com
docker compose exec -T app python -m app.cli reset-mfa --email someone@example.com [--passkeys]
docker compose exec -T app python -m app.cli trust-proxy --ip 10.0.2.58      # the Cloudflare Tunnel connector
docker compose exec app python -m app.cli create-admin --email you@twgsecurity.com   # also resets that admin's password
```

## How it works

```
 NX site A ─┬─ push: JSON-RPC websocket (rest.v4.events.log.subscribe) ─┐
            └─ poll: /rest/v4/events/log every 5 s (backstop) ──────────┼─> Postgres ─> SSE ─> browsers
 NX site … ─── (same, per site) ────────────────────────────────────────┘
       ▲
       └── acknowledge / bookmark / clips (portal -> NX, credentials never reach the browser)
```

### Alarm delivery: no silent delays

| Path | Normal | If it breaks |
|---|---|---|
| NX → portal | **push**, ~0.1 s (measured 87–219 ms on the TWG site) | the poll backstop runs every 5 s, and **every 1 s whenever push is down** (e.g. the ~10 s NX needs to set push up after a restart); a failed poll is retried after 1 s; push reconnects with backoff |
| Portal → browser | SSE, instant; 5 s heartbeat | the page reconnects on any error (including 502s during restarts); after 12 s of silence it polls open alarms every 2 s; after 10 s a red **LIVE UPDATES LOST** banner and a tone every 10 s |
| Site unreachable ≥ 60 s | — | a **Site connection lost** alarm (level Alarm, configurable), with the reconnect time noted on it |

Alarms picked up by any catch-up path are raised exactly like live ones: sound, pop-up and feed.
The top bar always shows **● Live** or **Reconnecting…**; the site panel shows **Live push** or **Polling only**.

NX quirk: over JSON-RPC, `startTimeMs` is ignored, and the subscribe returns the whole event log (104k rows / 145 MB on the TWG site). The portal subscribes with `limit=1`. NX still takes about 10 s to set the subscription up; polling covers that window.

**Camera-generated (ONVIF) analytics can arrive late.** Events such as "Object Class – Human", "Line Detector – Crossed" and "Audio Detected" are produced by the camera and collected by NX over ONVIF. They carry the camera's whole-second time stamp, and on the TWG site they reached the portal 2–8 s late, sometimes up to a minute. NX-native events (soft triggers) and NX server plugins (CVEDIA) arrive in about 0.1 s. The portal logs every pushed event's lag (`pushed … ms after its timestamp`) so the upstream delay is visible.

**NX rule setting that delays alarms:** a rule's *Interval of action* ("once in 1 min") makes NX hold back repeats inside that interval. Turn it off on rules that should alarm.

- **Pollers** (`app/services/poller.py`): one asyncio task per site reads new event-log rows every `POLL_INTERVAL_S`.
  - Each read re-covers the last 5 seconds, so late events aren't missed.
  - Rows are de-duplicated on a stable per-event key, because one NX event produces one log row per rule action.
  - A site that fails backs off on its own (up to 2 minutes between retries) and shows as offline. It never slows down the other sites.
- **What counts as an alarm** (`app/services/alarm_filter.py`):

  | Priority | Events |
  |---|---|
  | Critical | Any event from a rule with **Force acknowledgement** |
  | High | Security events: generic, soft trigger, camera input, analytics, object detected |
  | System | Health events: camera disconnected, storage/network/server issues, licensing |

  Motion, plugin diagnostics and server-started notices are ignored by default. `sites.alarm_types` can override the list for one site.
  **NX only logs events that some rule fired on**, so an event must be covered by at least one NX rule (a "Write to log" action is enough) to reach the portal.
- **Acknowledgement** (`app/services/ack.py`):
  - The ack is claimed atomically, so two operators can't both handle the same alarm.
  - Forced-ack alarms go to `POST /rest/v4/events/acknowledges`, which creates an NX bookmark and clears the notification in the NX Desktop client. Everything else gets a camera bookmark tagged `alarm-portal`.
  - If NX can't be reached, the local acknowledgement still stands, and the failure is shown and logged.
- **NX API client** (`app/nx/client.py`): copied from [nx-witness-mcp](https://github.com/TWG-Security/nx-witness-mcp) with a few small local changes, which are listed in the file header.

## How alarm video works

- The portal requests `GET /rest/v4/devices/{id}/media.mp4?positionMs&durationMs` from NX.
  - SD is the camera's secondary stream as recorded. This is fast, with no transcoding on NX.
  - HD is the primary stream, converted by NX to 720p H.264.
- NX starts the clip on the keyframe before the requested time and writes the true start time into the MP4 comment tag. ffprobe reads it, so boxes line up with the video to the frame.
- ffmpeg rewraps H.264 as-is. H.265 and MPEG-4 Part 2, which many NX secondary streams use, are converted to H.264 so every browser can play them. On the TWG sites, a 30 s clip including conversion takes about 2 s.
- **Growing clips:** video starts about 7 s after the alarm, not after the full window is recorded.
  - NX serves archive up to about 1 s behind live, but a request ending that close waits in real time. So a growing clip ends 3 s behind live and downloads in 1–3 s.
  - The first clip is built 5 s after the event (10 s before through 2 s after the alarm). It's extended every 5 s while someone is watching, and replaced by the full clip at about +24 s. The player keeps its position through each swap.
  - Measured on the TWG sites: **video playing 6.9 s after the event**, previously about 30 s.
- Clips are cached in the `mediacache` volume (4 GB cap, oldest evicted first) and served with HTTP range support, so the video can seek and loop.
- Bounding boxes come from `/rest/v4/analytics/objectTracks` plus each track's per-frame `objectMetadata`. Cameras whose analytics don't produce object tracks (for example camera-side ONVIF line crossing) have no boxes to draw.

## Alarm sound on operator workstations

Browsers block audio until someone clicks on the page. The portal shows a banner until that first click.
For an unattended monitoring screen, launch Chrome with `--autoplay-policy=no-user-gesture-required` (for example in a kiosk shortcut) so sound works right after a reload.

## NX account per site

Create a dedicated NX user for the portal on each site. It needs to be able to:
- view the event log
- view live video and archive on the cameras (for snapshots)
- add bookmarks

For NX-side acknowledgement to work, this user must also be one of the **target users** on any rule set to force acknowledgement.

To use rule `#tags`, the user must be allowed to read event rules (`GET /rest/v4/events/rules`). A 403 there means tags are ignored for that site, and the portal logs `could not read NX rules`.

## Deploy (Docker)

```bash
git clone https://github.com/TWG-Security/nx_alarm_portal.git && cd nx_alarm_portal
cp .env.example .env && chmod 600 .env   # fill in the three secrets
docker compose up -d --build
docker compose exec app python -m app.cli create-admin --email you@twgsecurity.com --name "Your Name"
```

Open `https://<PORTAL_HOST>`. Caddy serves it with its own internal certificate, so browsers will warn until you trust Caddy's root CA or give the portal a DNS name with a public certificate.

**Maps** use [Leaflet](https://leafletjs.com) (vendored in `app/static/vendor/leaflet`), map tiles from OpenStreetMap, and [Nominatim](https://nominatim.org) address search.
- Dark mode darkens the light OSM tiles with a CSS filter.
- Tiles are fetched by the portal (`/tiles/...`) and cached on disk for 7 days. Browsers never contact OSM directly, which follows OSM's tile usage policy regardless of browser referrer settings. Only signed-in users can fetch tiles, and at most 2 downloads run at once.
- Address search runs through `/api/geocode`, so the portal can follow Nominatim's policy: an identifying User-Agent, at most one request per second, and cached repeats.
- OSM's tile and search servers are donated and meant for light use. That is fine for an operator team, but for heavy use set `MAP_TILE_URL` and `GEOCODER_URL` to a commercial or self-hosted provider.

**Run a single app worker.** The pollers and the live-update bus run inside the process.

**Backups**: `tools/backup.sh` saves the database (`pg_dump`), `.env` (its `FERNET_KEY` decrypts the stored NX passwords) and checksums to `~/backups/nx_alarm_portal/<timestamp>/`.
- Each run proves the dump by restoring it into a scratch database and comparing row counts.
- It keeps the newest 14 (`KEEP`), and cron runs it nightly.
- To restore: `docker compose stop app`, then `docker compose exec -T db pg_restore -U portal -d portal --clean --if-exists < portal.dump`, then put `env.backup` back as `.env`.
- The backups hold secrets (mode 700/600). Keep any off-server copy encrypted.
- Everything is in the database: sites, alarms, users, sessions, two-step secrets, passkeys and Platform settings. The SMTP password, Cloudflare token, Google client secret and authenticator secrets are encrypted with `FERNET_KEY`, so restore `env.backup` together with the dump.

## Develop

```bash
uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements-dev.txt
cp .env.example .env.dev   # set SECRET_KEY, FERNET_KEY, DATABASE_URL=sqlite+aiosqlite:///./dev.db, COOKIE_SECURE=false
set -a; . ./.env.dev; set +a
alembic upgrade head
python -m app.cli create-admin --email you@twgsecurity.com
uvicorn app.main:app --reload --port 8099
pytest
```

`tools/dev_up.sh` starts a fresh dev stack (fake NX on :8199, portal on :8099). The dev login is `admin@twgsecurity.com` / `Smoke-test-password-123!`. Browser tests live in `tools/e2e/` (see the file headers). Passkeys work on `localhost` in dev (`WEBAUTHN_RP_ID=localhost`).

For browser testing without a real NX site, run the fake NX server in `tools/fake_nx.py`. It lets you fire panic, analytics and health events on demand; see the file header for how.

## Roadmap (not in this base)

3D globe view based on [God's Eye View](https://github.com/bilawalsidhu/gods-eye-view) · SNMP · off-server backup copy · per-company subdomains · claim/escalation workflow and SOPs per site · reports and CSV export · level rules by camera/keyword · multi-worker bus (Postgres LISTEN/NOTIFY) for zero-downtime deploys

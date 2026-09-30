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
  - Controls: play/pause, 0.1 s frame steps (← →), 0.25×–4× speed, jump to the alarm, widening the window 15 s earlier or later (up to 5 min), HD, live view and download.
  - **Analytics bounding boxes** are drawn over the video in sync, labeled with the object type and its first attribute. The event's own object is red; other objects in the clip are orange.
  - Clips for Critical and Alarm events are pre-built as soon as the footage exists, so they open instantly.
- **Acknowledge + log**: the operator writes a disposition note and clicks Acknowledge. The portal writes the ack back to NX (either clearing a forced-acknowledgement notification or adding a bookmark), and every step goes into an append-only audit log.
- **Add sites through the vmsproxy relay**: enter the site's Nx Cloud ID, the credentials and a map pin, then click **Connect**.
- Dark mode is the default; a light theme is available.
- Every table is tenant-scoped, so the portal is ready to go multi-tenant later.

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
| NX → portal | **push**, ~0.1 s (measured 87 ms on the TWG site) | 5 s poll backstop; a failed poll is retried after 1 s; push reconnects with backoff |
| Portal → browser | SSE, instant; 5 s heartbeat | the page reconnects on any error (including 502s during restarts); after 12 s of silence it polls open alarms every 2 s; after 10 s a red **LIVE UPDATES LOST** banner and a tone every 10 s |
| Site unreachable ≥ 60 s | — | a **Site connection lost** alarm (level Alarm, configurable), with the reconnect time noted on it |

Alarms picked up by any catch-up path are raised exactly like live ones: sound, pop-up and feed.
The top bar always shows **● Live** or **Reconnecting…**; the site panel shows **Live push** or **Polling only**.

NX quirk: over JSON-RPC, `startTimeMs` is ignored, and the subscribe returns the whole event log (104k rows / 145 MB on the TWG site). The portal subscribes with `limit=1`. NX still takes about 10 s to set the subscription up; polling covers that window.

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

For browser testing without a real NX site, run the fake NX server in `tools/fake_nx.py`. It lets you fire panic, analytics and health events on demand; see the file header for how.

## Roadmap (not in this base)

Tenant admin UI and tenant switching · Google Workspace SSO · recorded clip playback and live video · claim/escalation workflow and SOPs per site · reports and CSV export · NX "HTTP request" webhooks to cut latency below the poll interval · multi-worker bus (Postgres LISTEN/NOTIFY)

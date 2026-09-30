# TWG Alarm Portal

One place where TWG Security operators see alarms from every NX Witness deployment.

- **Map** (OpenStreetMap, no API key): every site is a pin, and the pin changes when the site needs attention:
  - **red, pulsing**: open security alarm
  - **amber**: open system-health issue only
  - **green**: all clear
  - **grey**: site offline or login failing
- **Unified alarm queue**: alarms from every site in one live list. A chime sounds on new alarms (can be muted). Each alarm shows the camera frame at the moment it happened, and a live view is one click away.
- **Acknowledge + log**: the operator writes a disposition note and clicks Acknowledge. The portal writes the ack back to NX (either clearing a forced-acknowledgement notification or adding a bookmark), and every step goes into an append-only audit log.
- **Add sites through the vmsproxy relay**: enter the site's Nx Cloud ID, the credentials and a map pin, then click **Connect**.
- Dark mode is the default; a light theme is available.
- Every table is tenant-scoped, so the portal is ready to go multi-tenant later.

## How it works

```
 NX site A ─┐                                   ┌─> browser (map)
 NX site B ─┼─ poller per site ─> Postgres ─> SSE bus ─> browser (alarm queue)
 NX site … ─┘  every 5 s, /rest/v4/events/log   └─> browser …
       ▲
       └── acknowledge / bookmark / snapshot (portal -> NX, credentials never reach the browser)
```

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

## NX account per site

Create a dedicated NX user for the portal on each site. It needs to be able to:
- view the event log
- view live video and archive on the cameras (for snapshots)
- add bookmarks

For NX-side acknowledgement to work, this user must also be one of the **target users** on any rule set to force acknowledgement.

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

## Roadmap (not in this base)

Tenant admin UI and tenant switching · Google Workspace SSO · recorded clip playback and live video · claim/escalation workflow and SOPs per site · reports and CSV export · NX "HTTP request" webhooks to cut latency below the poll interval · multi-worker bus (Postgres LISTEN/NOTIFY)

# Changelog

## [Unreleased]

### Added
- **Incident export**, from any alarm's details panel ("Export report / clip…"):
  - **Incident report (PDF)**, TWG-branded:
    - summary, with times in the site's time zone and UTC to the millisecond
    - the NX event-time frame, plus a sequence of stills from the clip (−5 s to +10 s) with analytics boxes drawn in
    - detected objects and their attributes
    - a timeline built from the audit log (event, arrival, the last arm/disarm before the event, acknowledgement and NX write-back, earlier exports)
    - the operator's disposition note, an optional note for the report, and the clip's SHA-256
  - **Evidence package (ZIP)**: the PDF, the MP4 clip, original and annotated stills, `manifest.json` and `SHA256SUMS.txt`.
  - Choice of clip window (default, ±30/60 s, ±60/120 s) and SD or HD.
  - Every export is audit-logged with its checksums. PDF building runs off the event loop; an alarm during an export arrived in 114 ms.
- **Site arming**:
  - Arm or disarm each site from the map's site panel or the Sites page.
  - A disarm can re-arm by itself: at the next scheduled arm, or after 30 min to 24 h. It can also be left until someone re-arms. The note is audit-logged.
  - Weekly **arming schedules** on the site form ("disarm 07:00 Mon–Fri, arm 18:00 Mon–Fri"), in the site's time zone and DST-aware. Saving a schedule never flips the state on the spot.
  - While a site is disarmed, security events are recorded ("While disarmed" filter on the Alarms page) but not raised. System alarms and NX rules tagged **`#24h`** still raise.
  - Disarmed sites show a DISARMED chip, a dashed map pin and a running "disarmed for" timer.
  - Scheduled and timer changes reach browsers live and are audit-logged (migration 0004).
- **NX rule health** on the map's site panel and the Sites page:
  - Alarm rules with NX's "Interval of action" set are flagged, because NX holds repeat events back and delivers them up to that interval late.
  - Also shown when the portal's NX account can't read rules at all.
- `tools/e2e/prod_probe.py`: measures alarm delivery over the LAN and the public (Cloudflare Tunnel) address side by side.

### Fixed
- The Sites page failed to load through Cloudflare after a deploy: Cloudflare turns `no-cache` into a 4-hour browser cache, so old and new scripts got mixed. Assets are now served from content-versioned URLs.
- Arming on the map's site panel is a clear card: ARMED or DISARMED, how long, who and why, when it changes next, and one full-width Arm or Disarm button.
- The public name `alarmportal.twgsecurity.net` showed a blank page: Caddy answered unknown hostnames with an empty 200. The name is now in the Caddyfile (`PUBLIC_HOST`).
- **Growing clips**: video starts about 7 s after an alarm (was about 30 s). The clip covers the footage recorded so far, is extended every 5 s in place, and is replaced by the full clip once recorded. Prefetch builds the first growing clip at +5 s.
- The portal logs the lag of every event NX pushes, which shows delays upstream of the portal.
- **Push delivery from NX**: a per-site JSON-RPC websocket (`rest.v4.events.log.subscribe`) gets alarms to the screen in about 0.1 s. Polling continues as a backstop, and a per-site lock serializes push and poll ingest.
- **No silent delays in the browser**:
  - auto-reconnect after any stream error, including the 502 that permanently killed EventSource during restarts
  - a 5 s heartbeat with a 12 s watchdog
  - polling open alarms every 2 s while the stream is down, with catch-up alarms raised with sound and pop-up
  - a red "LIVE UPDATES LOST" banner and a tone after 10 s
  - a "● Live" indicator in the top bar
- **Site connection lost** alarm when a site is unreachable for 60 s; the reconnect time is noted on it.
- Failed polls are retried after 1 s instead of a full cycle. Graceful shutdown is capped at 2 s, so restarts don't stall for 10 s.
- **Level per NX rule**: `#critical`, `#alarm`, `#warning` or `#ignore` in an NX rule's Title/Comment sets the level for that rule and overrides everything else. When several rules fire for one event, the loudest wins. The alarm details show where the level came from (`alarms.level_source`, migration 0003).
- **Alarm video clips**:
  - A looping clip around each alarm (default −10 s / +20 s) in the drawer and the critical pop-up.
  - Timeline with an alarm marker and object-presence marks; play/pause, frame step, speed, jump to alarm, widen earlier/later, HD, live and download.
  - NX's recorded start time keeps playback frame-accurate. Non-H.264 streams (H.265, MPEG-4 Part 2) are converted by ffmpeg.
  - Clips are cached on disk with range support, and pre-built for Critical and Alarm events.
- **Analytics bounding boxes** over the video, from NX object tracks and their per-frame metadata, in sync with playback.
- `tools/fake_nx.py` serves synthetic clips and moving object tracks.
- **Alarm levels**: Critical, Alarm, Warning or Ignore for each NX event type, set on an admin Settings page. Force-acknowledge NX rules are always Critical. Saving re-levels open alarms.
- **Audible alarms**:
  - Critical: a siren repeating every 4 s. Alarm: a chime repeating every 30 s. Warning: one tone.
  - "Silence 2 min" and a mute toggle.
  - A banner appears when the browser blocks sound.
  - Only one tab plays at a time.
- **Critical pop-up** on every page: site, address, camera, event-time frame, running timer, note and Acknowledge, Show on map, Silence, Minimize, and paging between several criticals.
- **Live alarm feed** on the overview page (right-hand column): level tabs with counts, a running "active for" timer on every card, and acknowledge or open details from the card.
- Soft-trigger alarms are named after the NX user who pressed them; events without a caption get readable labels.
- `tools/fake_nx.py` for end-to-end testing.

### Changed
- Maps moved from Google Maps to Leaflet + OpenStreetMap, so no API key is needed.
  - Tiles are darkened in dark mode.
  - Address search uses Nominatim through a rate-limited `/api/geocode` proxy.
  - The tile and geocoder URLs can be set in `.env`.

### Fixed
- Sites no longer flap offline on one-off relay 503s. A site shows offline only after 60 s of continuous failure; a bad password still shows at once.
- Map tiles were blocked by OpenStreetMap ("Access blocked"): browsers sent no Referer because of the portal's Referrer-Policy. Tiles are now proxied and cached server-side with an identifying User-Agent.
- Caddy `default_sni`, so HTTPS works when the portal is opened by IP address.

## [0.1.0] - 2026-09-29

### Added
- Base portal:
  - Map site view with alarm-aware pins
  - unified live alarm queue (SSE)
  - acknowledge with disposition notes and NX write-back (forced-ack clear or bookmark)
  - append-only audit log
- Sites are added through the vmsproxy relay (Nx Cloud ID) or a direct URL, with a Connect test. NX passwords are Fernet-encrypted at rest.
- Local accounts (argon2) with admin/operator roles, CSRF protection, and login throttling.
- Schema is tenant-scoped throughout (single "TWG Security" tenant seeded).
- Dark theme by default, plus a light theme, in TWG branding.
- Deployment with Docker Compose (app + Postgres 16 + Caddy HTTPS).

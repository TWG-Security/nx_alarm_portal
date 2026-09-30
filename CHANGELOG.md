# Changelog

## [Unreleased]

### Added
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

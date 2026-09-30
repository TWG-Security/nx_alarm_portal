# Changelog

## [Unreleased]

### Changed
- Maps moved from Google Maps to Leaflet + OpenStreetMap, so no API key is needed.
  - Tiles are darkened in dark mode.
  - Address search uses Nominatim through a rate-limited `/api/geocode` proxy.
  - The tile and geocoder URLs can be set in `.env`.

### Fixed
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

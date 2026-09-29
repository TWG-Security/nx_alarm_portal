# Changelog

## [0.1.0] - 2026-09-29

### Added
- Base portal:
  - Google Maps site view with alarm-aware pins
  - unified live alarm queue (SSE)
  - acknowledge with disposition notes and NX write-back (forced-ack clear or bookmark)
  - append-only audit log
- Sites are added through the vmsproxy relay (Nx Cloud ID) or a direct URL, with a Connect test. NX passwords are Fernet-encrypted at rest.
- Local accounts (argon2) with admin/operator roles, CSRF protection, and login throttling.
- Schema is tenant-scoped throughout (single "TWG Security" tenant seeded).
- Dark theme by default, plus a light theme, in TWG branding.
- Deployment with Docker Compose (app + Postgres 16 + Caddy HTTPS).

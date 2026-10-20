"""Runtime settings, read from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./portal.db"
    secret_key: str                      # signs session cookies
    fernet_key: str                      # encrypts NX site passwords at rest

    # Map tiles are fetched server-side (app/routers/tiles.py) and cached on disk.
    # Upstream is OpenStreetMap by default; any XYZ tile server works, e.g. a self-hosted one.
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tile_cache_dir: str = "./tile-cache"
    tile_cache_ttl_s: int = 7 * 24 * 3600   # OSM asks clients to cache for at least 7 days
    map_tile_attribution: str = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    map_tile_max_zoom: int = 19
    # Aerial imagery for the 3D site view, also proxied and cached. USGS The National Map is public
    # domain (fine for commercial use) but US-only at high zoom. Levels up to sat_tile_max_tiled come
    # from the tiled cache (fast); deeper ones from NAIP (0.3 m) rendered per tile (1-8 s, then cached).
    sat_tile_url: str = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
    sat_tile_max_tiled: int = 16
    sat_hires_url: str = ("https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPPlus/ImageServer/exportImage"
                          "?bbox={bbox}&bboxSR=3857&imageSR=3857&size=256,256&format=jpg&f=image")
    sat_tile_max_zoom: int = 19
    sat_tile_cache_ttl_s: int = 30 * 24 * 3600
    sat_tile_attribution: str = "USGS The National Map: Orthoimagery"
    # Google Photorealistic 3D Tiles (real 3D buildings) for the 3D site view. A browser key
    # (Map Tiles API, restricted to the portal's web addresses): browsers fetch the tiles from
    # Google directly, so site locations go to Google. Empty = USGS imagery on a flat globe.
    google_map_tiles_key: str = ""
    # Address lookup (OSM Nominatim by default; policy: max 1 request/second, identify the app).
    geocoder_url: str = "https://nominatim.openstreetmap.org/search"
    geocoder_user_agent: str = "TWG-Alarm-Portal/0.1 (+https://github.com/TWG-Security/nx_alarm_portal)"

    push_enabled: bool = True            # NX JSON-RPC websocket push (sub-second alarms); polling stays as backstop
    poll_interval_s: float = 5.0         # backstop: how often each site's event log is also read
    poll_retry_s: float = 1.0            # a failed poll is retried this fast (relay 503s are usually one-off)
    poll_no_push_s: float = 1.0          # poll this fast whenever a site's push is down (e.g. ~10 s after a restart)
    poll_overlap_ms: int = 5000          # re-read window so late-arriving events are not missed
    poll_max_backoff_s: float = 120.0    # ceiling for retry delay on a failing site
    offline_after_s: float = 60.0        # only show a site offline after failing this long (relay 503s are often one-off)
    initial_lookback_ms: int = 0         # on first connect, how far back to pull events (0 = from now)
    device_refresh_s: float = 600.0      # camera-name cache refresh
    rules_refresh_s: float = 60.0        # how quickly NX rule #tag edits take effect

    nx_timeout_s: float = 15.0

    # Arming (app/services/arming.py). Schedules run in each site's time zone; sites without one use this.
    default_timezone: str = "America/New_York"
    arm_tick_s: float = 1.0              # how often scheduled arm/disarm changes are announced

    # Alarm video clips (app/services/clips.py)
    clip_pre_s: int = 10                 # default window: this long before the alarm...
    clip_post_s: int = 20                # ...to this long after
    clip_max_window_s: int = 300         # operators can widen the window up to this total
    media_cache_dir: str = "./media-cache"
    media_cache_max_mb: int = 4096       # oldest clips are evicted beyond this
    # Guard tracking: Peplink InControl2 API (dev points it at tools/fake_nx.py's fake IC2)
    peplink_api_base: str = "https://api.ic.peplink.com"
    # Fleet vehicles from Fleetio (app/fleetio.py; dev points it at tools/fake_nx.py's fake Fleetio)
    fleetio_api_base: str = "https://secure.fleetio.com/api"
    # Site 3D models (app/routers/site_models.py): uploads and their converted 3D Tiles
    model_dir: str = "./site-models"
    model_max_mb: int = 3072             # largest upload
    model_build_max_s: int = 1800        # a conversion is stopped after this long
    model_build_mem_mb: int = 2048       # memory cap for the converter process (the server has 4 GB)
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    prefetch_clips: bool = True          # build clips for critical/alarm events as soon as footage exists
    # The signed cookie's own lifetime. Sessions end on the server (app/sessions.py: closed browser, max age, revoked);
    # this only has to outlive the longest "closed browser" setting (90 days). It was 12 h counted from sign-in and
    # never renewed, which signed every screen out 12 h after login however busy it was (fixed 2026-10-15).
    session_max_age_s: int = 90 * 24 * 3600

    # Sign-in protection (app/security_guard.py, app/net.py). Both also editable on the Platform page.
    trusted_proxy_ips: str = ""          # comma-separated IPs/CIDRs allowed to send CF-Connecting-IP (the tunnel connector)
    ip_allowlist: str = ""               # comma-separated IPs/CIDRs that are never banned
    # Passkeys (WebAuthn) only work on this exact host name; the page hides them elsewhere (e.g. the LAN IP).
    webauthn_rp_id: str = "alarmportal.twgsecurity.net"
    webauthn_origins: str = "https://alarmportal.twgsecurity.net"   # comma-separated
    # The iPhone app (docs/IOS_APP_BRIEF.md): served in /.well-known/apple-app-site-association so iOS offers the
    # portal's saved passwords and passkeys in the app. APPLE_TEAM_ID = the 10-character Apple Developer Team ID.
    apple_team_id: str = ""
    apple_bundle_id: str = "com.twgsecurity.alarms"
    # Push notifications for the iPhone app (app/push.py): token-based APNs auth. The .p8 file lives next to .env on
    # the host (mode 600, never in git or the backups) and is mounted into the container read-only.
    apns_key_path: str = ""
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_topic: str = "com.twgsecurity.alarms"
    apns_api_base: str = ""          # "" = Apple's (production / sandbox per device); dev and tests point it at a fake
    webauthn_rp_name: str = "TWG Alarm Portal"
    cookie_secure: bool = True           # set False only for plain-http local dev
    twilio_api_base: str = "https://api.twilio.com"   # text messages (app/sms.py); tests and dev point it at a fake
    start_pollers: bool = True           # tests turn this off
    # Camera image health (app/camhealth/worker.py, the `vision` container)
    camhealth_interval_s: int = 900      # one frame per camera this often (the user, 2026-10-20: 15 min is fine)
    camhealth_parallel: int = 1          # cameras checked at once over all sites (one at a time: the GPU has 4 GB)
    camhealth_frame_width: int = 1280    # px asked from NX
    camhealth_dir: str = "camhealth-data"   # the per-time-of-day frame library (vision container volume)
    camhealth_ai: str = "auto"           # auto (GPU if there is one) | off
    camhealth_watch_s: float = 10.0      # the web app looks for changed cameras this often (-> camera.health events)


@lru_cache
def get_settings() -> Settings:
    return Settings()

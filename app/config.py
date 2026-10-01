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
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    prefetch_clips: bool = True          # build clips for critical/alarm events as soon as footage exists
    session_max_age_s: int = 12 * 3600

    # Sign-in protection (app/security_guard.py, app/net.py). Both also editable on the Platform page.
    trusted_proxy_ips: str = ""          # comma-separated IPs/CIDRs allowed to send CF-Connecting-IP (the tunnel connector)
    ip_allowlist: str = ""               # comma-separated IPs/CIDRs that are never banned
    # Passkeys (WebAuthn) only work on this exact host name; the page hides them elsewhere (e.g. the LAN IP).
    webauthn_rp_id: str = "alarmportal.twgsecurity.net"
    webauthn_origins: str = "https://alarmportal.twgsecurity.net"   # comma-separated
    webauthn_rp_name: str = "TWG Alarm Portal"
    cookie_secure: bool = True           # set False only for plain-http local dev
    start_pollers: bool = True           # tests turn this off


@lru_cache
def get_settings() -> Settings:
    return Settings()

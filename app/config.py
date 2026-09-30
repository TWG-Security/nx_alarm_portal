"""Runtime settings, read from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./portal.db"
    secret_key: str                      # signs session cookies
    fernet_key: str                      # encrypts NX site passwords at rest

    # Map tiles (OpenStreetMap by default; any XYZ tile server works, e.g. a self-hosted one).
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_tile_attribution: str = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    map_tile_max_zoom: int = 19
    # Address lookup (OSM Nominatim by default; policy: max 1 request/second, identify the app).
    geocoder_url: str = "https://nominatim.openstreetmap.org/search"
    geocoder_user_agent: str = "TWG-Alarm-Portal/0.1 (+https://github.com/TWG-Security/nx_alarm_portal)"

    poll_interval_s: float = 5.0         # how often each site's event log is read
    poll_overlap_ms: int = 5000          # re-read window so late-arriving events are not missed
    poll_max_backoff_s: float = 120.0    # ceiling for retry delay on a failing site
    initial_lookback_ms: int = 0         # on first connect, how far back to pull events (0 = from now)
    device_refresh_s: float = 600.0      # camera-name cache refresh

    nx_timeout_s: float = 15.0
    session_max_age_s: int = 12 * 3600
    cookie_secure: bool = True           # set False only for plain-http local dev
    start_pollers: bool = True           # tests turn this off


@lru_cache
def get_settings() -> Settings:
    return Settings()

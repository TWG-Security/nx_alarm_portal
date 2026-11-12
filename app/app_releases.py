"""The phone apps' downloads: the newest Android APK from the private GitHub repo's latest release, cached on disk.

GET /repos/{repo}/releases/latest (at most once an hour; a failed check retries after RETRY_S), then the release's .apk
asset with Accept: application/octet-stream. The file and its details live in MEDIA_CACHE_DIR/apps/ (the mediacache
volume): a GitHub outage keeps serving the last good APK. The token (GITHUB_RELEASES_TOKEN) and GitHub's URLs never
leave the server; httpx drops the Authorization header on the redirect to GitHub's file storage.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import get_settings

log = logging.getLogger("portal.apps")

CHECK_EVERY_S = 3600
RETRY_S = 300                 # after a failed check
MAX_BYTES = 300 * 2**20
_lock = asyncio.Lock()


@dataclass
class AndroidRelease:
    version: str              # "0.1" (from the asset name TWG-Alarms-0.1.apk, else the tag)
    tag: str
    name: str
    published_at: str         # ISO time from GitHub
    asset_id: int
    asset_name: str
    size: int
    sha256: str = ""

    @property
    def filename(self) -> str:
        return f"TWG-Alarms-{self.version}.apk"


def _dir() -> Path:
    return Path(get_settings().media_cache_dir) / "apps"


def _meta_path() -> Path:
    return _dir() / "android.json"


def _apk_path() -> Path:
    return _dir() / "android.apk"


def _read_meta() -> dict:
    try:
        return json.loads(_meta_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_meta(meta: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    tmp = _meta_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, _meta_path())


def version_of(asset_name: str, tag: str) -> str:
    m = re.match(r"^TWG-Alarms-(.+)\.apk$", asset_name)
    if m:
        return m.group(1)
    return re.sub(r"^v", "", tag).split("-")[0] or tag


def _client() -> httpx.AsyncClient:
    cfg = get_settings()
    return httpx.AsyncClient(base_url=cfg.github_api_base, timeout=httpx.Timeout(20, read=120), follow_redirects=True,
                             headers={"Authorization": f"Bearer {cfg.github_releases_token}",
                                      "User-Agent": "TWG-Alarm-Portal", "X-GitHub-Api-Version": "2022-11-28"})


async def _check(meta: dict) -> dict:
    """Ask GitHub for the latest release; download its APK when it's new. Returns the new meta."""
    repo = get_settings().android_repo
    async with _client() as gh:
        r = await gh.get(f"/repos/{repo}/releases/latest", headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        rel = r.json()
        asset = next((a for a in rel.get("assets") or [] if a.get("name", "").endswith(".apk")), None)
        if asset is None:
            raise RuntimeError(f"release {rel.get('tag_name')} has no .apk")
        release = AndroidRelease(version=version_of(asset["name"], rel.get("tag_name", "")), tag=rel.get("tag_name", ""),
                                 name=rel.get("name") or "", published_at=rel.get("published_at") or "",
                                 asset_id=asset["id"], asset_name=asset["name"], size=asset.get("size") or 0)
        if meta.get("asset_id") == release.asset_id and meta.get("sha256") and _apk_path().exists():
            return {**meta, "checked_at": time.time(), "failed_at": 0}
        _dir().mkdir(parents=True, exist_ok=True)
        tmp = _apk_path().with_suffix(".part")
        digest, n = hashlib.sha256(), 0
        async with gh.stream("GET", f"/repos/{repo}/releases/assets/{release.asset_id}",
                             headers={"Accept": "application/octet-stream"}) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as out:
                async for chunk in resp.aiter_bytes(1 << 16):
                    n += len(chunk)
                    if n > MAX_BYTES:
                        raise RuntimeError("APK larger than expected")
                    digest.update(chunk)
                    out.write(chunk)
        if release.size and n != release.size:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"APK download incomplete ({n} of {release.size} bytes)")
        release.size, release.sha256 = n, digest.hexdigest()
        os.replace(tmp, _apk_path())
        log.info("Android app %s (%s) cached: %d bytes, sha256 %s", release.version, release.tag, n, release.sha256[:16])
        return {**release.__dict__, "checked_at": time.time(), "failed_at": 0}


def _usable(meta: dict, need_file: bool) -> bool:
    return bool(meta.get("asset_id")) and (not need_file or (bool(meta.get("sha256")) and _apk_path().exists()))


def _due(meta: dict, need_file: bool) -> bool:
    """Ask GitHub now? Hourly, or at once when what's needed is missing; never within RETRY_S of a failure."""
    if time.time() - meta.get("failed_at", 0) < RETRY_S:
        return False
    return time.time() - meta.get("checked_at", 0) >= CHECK_EVERY_S or not _usable(meta, need_file)


def _why(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"GitHub answered HTTP {exc.response.status_code}"
    return exc.__class__.__name__ if isinstance(exc, httpx.HTTPError) else str(exc)


async def android(*, need_file: bool = False) -> AndroidRelease | None:
    """The newest Android release we have (checked against GitHub at most hourly). None = nothing usable yet (GitHub
    unreachable or no token). need_file: the APK must be on disk (the download)."""
    meta = _read_meta()
    if get_settings().github_releases_token and _due(meta, need_file):
        async with _lock:
            meta = _read_meta()                    # someone else may have just checked
            if _due(meta, need_file):
                try:
                    meta = await _check(meta)
                except Exception as exc:  # noqa: BLE001 - keep serving the last good APK
                    log.warning("Android app release check failed: %s", _why(exc))
                    meta = {**meta, "failed_at": time.time()}
                _write_meta(meta)
    if not _usable(meta, need_file):
        return None
    return AndroidRelease(**{k: meta[k] for k in AndroidRelease.__dataclass_fields__ if k in meta})


def apk_path() -> Path:
    return _apk_path()

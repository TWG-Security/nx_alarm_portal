"""Is this server's clock right? Every alarm time, clip window and incident report depends on it.

The host keeps time with NTP (systemd-timesyncd → ntp.ubuntu.com; checked 2026-10-13: synchronized, +0.04 ms). This
watches that from inside the app: every CHECK_EVERY_S it asks public NTP servers (SNTP, UDP 123) how far off the
clock is, logs loudly past MAX_OFFSET_MS, and pages show it (the top-bar clock, GET /api/time). It never sets the
clock (the host's NTP does that) and never blocks anything: the query runs in a thread with a short timeout.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time

log = logging.getLogger("portal.clock")

SERVERS = ("time.cloudflare.com", "time.google.com")
CHECK_EVERY_S = 600
MAX_OFFSET_MS = 1000
TIMEOUT_S = 3.0
NTP_EPOCH = 2208988800                       # seconds from 1900 (NTP) to 1970 (Unix)
PORT = 123

state: dict = {"ok": None, "offset_ms": None, "rtt_ms": None, "server": "", "checked_at": None, "error": ""}


def _ts(data: bytes, at: int) -> float:
    sec, frac = struct.unpack("!II", data[at:at + 8])
    return sec - NTP_EPOCH + frac / 2**32


def query(host: str, timeout: float = TIMEOUT_S) -> tuple[float, float]:
    """One SNTP exchange: (offset of this clock in seconds, + = server ahead of us; round trip in seconds)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t0 = time.time()
        s.sendto(b"\x23" + 47 * b"\0", (host, PORT))         # LI 0, version 4, mode 3 (client)
        data, _ = s.recvfrom(512)
        t3 = time.time()
    if len(data) < 48 or (data[0] & 0x07) != 4 or data[1] == 0:   # not a server reply, or stratum 0 (kiss-of-death)
        raise ValueError("not a usable NTP reply")
    t1, t2 = _ts(data, 32), _ts(data, 40)                   # server received / sent
    return ((t1 - t0) + (t2 - t3)) / 2, (t3 - t0) - (t2 - t1)


def check() -> dict:
    """Ask the servers in turn; the first answer counts. Updates and returns `state`."""
    errors = []
    for host in SERVERS:
        try:
            offset, rtt = query(host)
        except (OSError, ValueError) as e:
            errors.append(f"{host}: {e}")
            continue
        was = state.get("ok")
        state.update(ok=abs(offset) * 1000 <= MAX_OFFSET_MS, offset_ms=round(offset * 1000, 1), rtt_ms=round(rtt * 1000, 1),
                     server=host, checked_at=time.time(), error="")
        if not state["ok"]:
            log.error("CLOCK: this server is %.0f ms off %s (NTP); alarm times and clips may be wrong. "
                      "Check: timedatectl timesync-status", offset * 1000, host)
        elif was is False:
            log.warning("CLOCK: back in sync with %s (%.1f ms)", host, offset * 1000)
        return state
    state.update(ok=None, offset_ms=None, rtt_ms=None, server="", checked_at=time.time(), error="; ".join(errors)[:300])
    log.warning("CLOCK: no NTP server answered (%s); the host's own NTP may still be fine", state["error"])
    return state


async def run() -> None:
    """Background loop (main.lifespan)."""
    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(check)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let the clock check die
            log.exception("clock check failed")
        await asyncio.sleep(CHECK_EVERY_S)


def public() -> dict:
    return {"now_ms": int(time.time() * 1000),
            "ntp": {"ok": state["ok"], "offset_ms": state["offset_ms"], "server": state["server"],
                    "checked_ms": int(state["checked_at"] * 1000) if state["checked_at"] else None, "error": state["error"]}}

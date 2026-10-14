"""The server-clock check (app/services/clock.py) against a fake NTP server, and GET /api/time."""
import socket
import struct
import threading
import time

import pytest

from app.services import clock


def fake_ntp(skew_s: float, reply=True):
    """A one-shot SNTP server on localhost whose clock is `skew_s` ahead of ours. Returns its port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    def serve():
        data, addr = s.recvfrom(512)
        if reply:
            now = time.time() + skew_s + clock.NTP_EPOCH
            ts = struct.pack("!II", int(now), int((now % 1) * 2**32))
            s.sendto(b"\x24\x02" + b"\0" * 30 + ts + ts, addr)          # mode 4 (server), stratum 2, receive + transmit
        s.close()
    threading.Thread(target=serve, daemon=True).start()
    return s.getsockname()[1]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(clock, "state", {**clock.state, "ok": None, "offset_ms": None})
    monkeypatch.setattr(clock, "TIMEOUT_S", 0.5)


def test_an_accurate_clock_passes(monkeypatch):
    monkeypatch.setattr(clock, "PORT", fake_ntp(0.0))
    monkeypatch.setattr(clock, "SERVERS", ("127.0.0.1",))
    st = clock.check()
    assert st["ok"] is True and abs(st["offset_ms"]) < 50 and st["server"] == "127.0.0.1"


def test_a_clock_off_by_more_than_a_second_is_flagged(monkeypatch, caplog):
    monkeypatch.setattr(clock, "PORT", fake_ntp(2.5))
    monkeypatch.setattr(clock, "SERVERS", ("127.0.0.1",))
    st = clock.check()
    assert st["ok"] is False and 2400 < st["offset_ms"] < 2600
    assert "CLOCK: this server is" in caplog.text


def test_no_ntp_answer_is_unknown_not_wrong(monkeypatch):
    monkeypatch.setattr(clock, "PORT", fake_ntp(0, reply=False))
    monkeypatch.setattr(clock, "SERVERS", ("127.0.0.1",))
    st = clock.check()
    assert st["ok"] is None and st["offset_ms"] is None and "127.0.0.1" in st["error"]


async def test_api_time(client):
    r = await client.get("/api/time")
    body = r.json()
    assert r.status_code == 200 and abs(body["now_ms"] - time.time() * 1000) < 2000 and "ntp" in body
    assert r.headers["cache-control"] == "no-store"

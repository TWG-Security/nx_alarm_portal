"""Stand-ins for wireproxy, tailscaled and tailscale (app/tunnels.py), for tests and the dev stack: a real SOCKS5
proxy that connects straight out, so the portal's path through a "VPN" is exercised end to end without WireGuard.
Each connection it carries is appended to <dir>/connections.log ("host:port"), so a test can see the traffic went
through the tunnel. Mode = the first argument (the wrappers next to this file pass it).

  wireproxy -c FILE -i ADDR     SOCKS5 on [Socks5] BindAddress; ADDR/metrics says last_handshake_time_sec=now
                                (an Endpoint containing "unreachable" never handshakes)
  tailscaled --statedir=D --socket=S --socks5-server=ADDR ...
  tailscale --socket=S up --auth-key=file:F ... | status --json | logout    (a key containing "bad" is refused)
"""
import asyncio
import ipaddress
import json
import re
import struct
import sys
import time
from pathlib import Path


async def socks(bind: str, log: Path):
    host, port = bind.rsplit(":", 1)

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            ver, n = await r.readexactly(2)
            await r.readexactly(n)
            w.write(b"\x05\x00")
            _, cmd, _, atyp = await r.readexactly(4)
            if atyp == 1:
                dst = str(ipaddress.ip_address(await r.readexactly(4)))
            elif atyp == 4:
                dst = str(ipaddress.ip_address(await r.readexactly(16)))
            else:
                dst = (await r.readexactly((await r.readexactly(1))[0])).decode()
            dport = struct.unpack("!H", await r.readexactly(2))[0]
            with log.open("a") as f:
                f.write(f"{dst}:{dport}\n")
            try:
                ur, uw = await asyncio.wait_for(asyncio.open_connection(dst, dport), 5)
            except Exception:  # noqa: BLE001
                w.write(b"\x05\x05\x00\x01" + bytes(6))      # connection refused
                await w.drain(); w.close(); return
            w.write(b"\x05\x00\x00\x01" + bytes(6))
            await w.drain()

            async def pipe(a, b):
                try:
                    while data := await a.read(65536):
                        b.write(data); await b.drain()
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    b.close()
            await asyncio.gather(pipe(r, uw), pipe(ur, w))
        except Exception:  # noqa: BLE001
            w.close()

    return await asyncio.start_server(handle, host, int(port))


async def wireproxy(args):
    conf = Path(args[args.index("-c") + 1]).read_text()
    info = args[args.index("-i") + 1]
    bind = re.search(r"BindAddress = (\S+)", conf).group(1)
    shakes = "unreachable" not in re.search(r"Endpoint = (\S+)", conf).group(1)
    log = Path(args[args.index("-c") + 1]).parent / "connections.log"
    server = await socks(bind, log)

    async def metrics(r, w):
        await r.readline()
        body = f"last_handshake_time_sec={int(time.time()) if shakes else 0}\n".encode()
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        await w.drain(); w.close()
    ih, ip = info.rsplit(":", 1)
    await asyncio.start_server(metrics, ih, int(ip))
    print("fake wireproxy up", flush=True)
    await server.serve_forever()


def opt(args, name):
    for a in args:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return ""


async def tailscaled(args):
    state = Path(opt(args, "statedir"))
    server = await socks(opt(args, "socks5-server"), state / "connections.log")
    Path(opt(args, "socket")).touch()
    await server.serve_forever()


def tailscale(args):
    state = Path(opt(args, "socket")).parent / "logged-in"
    if "up" in args:
        key = Path(opt(args, "auth-key").removeprefix("file:")).read_text()
        if "bad" in key:
            print("backend error: invalid key: unable to validate API key", flush=True)
            sys.exit(1)
        state.write_text(opt(args, "hostname"))
    elif "status" in args:
        print(json.dumps({"BackendState": "Running" if state.exists() else "NeedsLogin",
                          "Self": {"TailscaleIPs": ["100.64.0.7"] if state.exists() else []}}))
    elif "logout" in args:
        state.unlink(missing_ok=True)
        (state.parent / "logged-out").touch()


if __name__ == "__main__":
    mode, rest = sys.argv[1], sys.argv[2:]
    if mode == "tailscale":
        tailscale(rest)
    else:
        asyncio.run({"wireproxy": wireproxy, "tailscaled": tailscaled}[mode](rest))

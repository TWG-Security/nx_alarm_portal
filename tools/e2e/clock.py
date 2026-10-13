"""The top-bar clock shows the server's (NTP) time even when this PC's clock is wrong, flags the PC's clock, and alarm
"active for" timers stay right.

    POLL_INTERVAL_S=60 tools/dev_up.sh && .venv/bin/python -m tools.e2e.clock [screenshot_dir]
"""
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

from tools.e2e.common import inject, login_and_add_site

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
fails = []
SKEW_S = -90                     # this "PC" runs 90 s slow


def check(ok, msg):
    print(("PASS  " if ok else "FAIL  ") + msg)
    if not ok:
        fails.append(msg)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    # Shift the page's clock (Date.now / new Date) 90 s back, still ticking.
    pg.add_init_script(f"""(() => {{ const skew = {SKEW_S * 1000}, D = Date, now = D.now.bind(D);
      class Shifted extends D {{ constructor(...a) {{ super(...(a.length ? a : [now() + skew])); }} static now() {{ return now() + skew; }} }}
      window.Date = Shifted; }})()""")
    login_and_add_site(pg)
    pg.wait_for_function("document.getElementById('sys-clock').textContent !== '--:--:--'", timeout=10000)
    pg.wait_for_function("document.getElementById('sys-clock').classList.contains('sys-clock--warn')", timeout=10000)
    shown = pg.inner_text("#sys-clock")
    real = datetime.now().astimezone()
    print(f"      clock shows {shown!r}, real time {real:%I:%M:%S %p}")
    hh, mm, ss = [int(x) for x in shown.split()[0].split(":")]
    secs = lambda h, m, s: (h % 12) * 3600 + m * 60 + s  # noqa: E731
    diff = abs(secs(hh, mm, ss) - secs(real.hour, real.minute, real.second))
    check(min(diff, 43200 - diff) <= 2, f"the clock shows server time although this PC is 90 s slow ({shown})")
    title = pg.get_attribute("#sys-clock", "title")
    check("90 s slow" in title or "89 s slow" in title or "91 s slow" in title, f"it says the PC's clock is off ({title.splitlines()[-1]!r})")
    check("Server clock" in title, f"and how the server's clock stands ({title.splitlines()[1]!r})")
    # A brand-new alarm's timer starts near 0:00 (with the PC's clock it would start at 1:30).
    inject("line")
    pg.wait_for_selector(".feed-item .timer", timeout=10000)
    time.sleep(1.2)
    t = pg.inner_text(".feed-item .timer")
    m, s = [int(x) for x in t.split(":")[-2:]]
    check(m * 60 + s < 20, f"a new alarm's timer starts near 0:00 ({t})")
    pg.locator(".topbar").screenshot(path=f"{out}/clock-topbar.png")
    check(not errors, f"no page errors {errors}")
    b.close()

print("\nALL PASS" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)

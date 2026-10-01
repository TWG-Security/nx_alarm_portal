"""Shared helpers for the browser end-to-end tests (run against tools/dev_up.sh)."""
import urllib.request

PORTAL = "http://localhost:8099"
FAKE_NX = "http://127.0.0.1:8199"
LOGIN = ("admin@twgsecurity.com", "Smoke-test-password-123!")
# Counts Web Audio oscillators so tests can tell that alarm sounds actually played.
COUNT_TONES = """(() => { const o = AudioContext.prototype.createOscillator; window.__osc = 0;
    AudioContext.prototype.createOscillator = function () { window.__osc++; return o.apply(this, arguments); }; })()"""
BOX_PIXELS = """(sel) => { const c = document.querySelector(sel + ' .player-boxes'); const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
                   let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; }"""


def inject(kind: str) -> None:
    """kind: panic (critical soft trigger) | line (analytics alarm) | dock (camera-disconnected warning)"""
    urllib.request.urlopen(urllib.request.Request(f"{FAKE_NX}/_inject/{kind}", method="POST"))


def login_and_add_site(pg) -> None:
    pg.goto(f"{PORTAL}/login")
    pg.fill("#email", LOGIN[0]); pg.fill("#password", LOGIN[1])
    pg.click("button[type=submit]"); pg.wait_for_url(f"{PORTAL}/")
    pg.evaluate("""async () => { const t = document.querySelector('meta[name=csrf-token]').content;
        await fetch('/api/sites', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':t},
          body: JSON.stringify({name:'Fake Test Site', host:'http://127.0.0.1:8199', nx_user:'portal', nx_pass:'x',
                                address:'Harrisburg, PA', lat:40.2665, lng:-76.8838})}); }""")
    pg.reload()
    pg.mouse.click(600, 400)       # user gesture: browsers block sound until one

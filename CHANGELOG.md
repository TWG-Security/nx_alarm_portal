# Changelog

## [Unreleased]

### Changed
- **Users page tidied:** a compact table (name and email together, role, status, alarm-text summary with opt-in state, two-step, last sign-in) with one **Edit** button per person; no more sideways scrolling. **Edit** opens a dialog with name, role, mobile number, which alarms text them, opt-in state with Send opt-in / Send a test text, and the account actions (Disable/Enable, Reset password or New invite link, Sign-in security). **+ Add user** opens the add form in a dialog (it stays open to show the setup link).

### Added
- **Alarm texts for some or all sites** (migration 0015): each person's Edit / Add user dialog has **Sites that text them: All sites** (default, including sites added later) or **Only these sites** with a checkbox per site. Alarms from other sites don't text them. The Users table shows "All sites", the site's name, or "N sites" (hover for the list). Only the company's own sites can be picked.
- **Text-message consent, readable texts, alarm pictures** (migration 0014):
  - **Opt in by replying YES:** adding a mobile number (Users page) sends one text: "… Reply YES to sign up. Msg frequency varies. Msg & data rates may apply. Reply HELP for help, STOP to opt out at any time. Terms: …/sms-terms". **No alarm texts until they reply YES.** Users page shows *Not asked yet / Waiting for YES / Opted in / Replied STOP*, with **Send opt-in** (or **Resend**); **Test** only once opted in. A changed number is asked again.
  - **Replies** come in on `POST /twilio/sms` (Twilio's "A message comes in" webhook, checked with Twilio's signature): YES/START opts in and gets a confirmation; STOP (and STOPALL, UNSUBSCRIBE, CANCEL, END, QUIT) opts out; HELP gets the help text with the **Help contact** (Platform page). Twilio error 21610 also marks someone opted out. Opt-ins/outs are audit-logged (`sms.opted_in`, `sms.opted_out`) and in the texts log.
  - **Public terms page `/sms-terms`** (program, how to opt in/out, help, frequency, rates, carriers not liable, privacy: numbers never sold or shared for marketing), linked from the sign-in page and every opt-in text.
  - **Readable alarm texts**, one line each: alarm, site name, address, camera and time, **Directions** (a Google Maps link: tap to navigate), View alarm link, "Reply STOP to opt out." The alarm link page has a **Directions** button too.
  - **Alarm picture (MMS):** the frame at the alarm moment with its Person/Vehicle boxes. The text waits at most 4 s for it, then goes without; if Twilio refuses the picture the text is sent again without it. Platform switch, on by default.
  - **Background texts can't get stuck:** every text task has a 2-minute limit (a waiting summary: its 10-minute window + 2 min), then it's stopped and logged; app shutdown waits 2 s, then cancels them. Platform page shows texts in progress and warns past 60 s.
- **Alarm text messages (Twilio)** (migration 0013). Platform page → **Text messages**: Account SID, Auth Token (stored encrypted, never shown), the number to send from (or a Messaging Service SID), how long links work (default 24 h), the most texts per person in 10 minutes (default 10; past that one summary text counts the rest), a test button and a log of every text with its delay. **Users page:** each person gets a **mobile number** and **Alarm texts: No texts / Critical only / Critical + Alarm**, plus a **Test** button and their last text's result (a failure shows in red). People can get texts without ever signing in to the portal. Warnings never text; another company's alarms never text (same rule as sound).
  - Each text carries a link, `https://alarmportal.twgsecurity.net/a/<24 random characters>`, that opens **that one alarm without signing in**: details, the clip with boxes, the camera live, and "Sign in to acknowledge". Built for phones. Only a hash of the link is stored; it stops working when it expires, or at once if the person is disabled or the site archived. Guessing links counts as failed sign-ins (bans). Opening one is audit-logged (`alarm.link_opened`) under the person's name. Pages are never cached, indexed or sent as a Referer.
  - **Alarm safety:** texts go out in the background after the alarm is on screens. Measured in dev with the fake Twilio: alarms on screen in 29-44 ms with texts on, 77 ms while Twilio rejected every text, 29-72 ms while Twilio hung; text accepted by Twilio 0.1 s after the alarm reached the portal. Twilio errors retry (network, 429, 5xx: 3 tries) and show on the Platform page and the Users page. e2e: `tools/e2e/sms.py`; tests `tests/test_sms.py`.
- **Cones on/off** on the top-down view's toolbar: hides the cameras' view cones, leaving their dots and names. A camera with an open alarm still shows its red flashing cone. Remembered per browser.
- **Clicking a site goes straight to its top-down view** (site list or its pin on the map), with its cameras. Clicking an alarm does the same behind the details, centred on the alarm's camera (red). **Exit top-down**, the site panel's ✕ or "Show all sites" go back to the street-map overview. In the 3D view, clicking a site still flies the 3D view there.
- **Live preview on hover (top-down view):** rest the mouse on a camera's dot or cone and a small live window opens beside it: a still at once, live video about 0.4 s later. It closes, and its stream stops, when the mouse leaves. Sweeping across the map doesn't open any (0.25 s delay); none while placing cameras. Click still pins the bigger live window.
- **Where the alarm is, from above:** the alarm details now show the site's top-down map beside the details, centred on the alarm's camera with its cone **red and flashing** (other cameras orange, red if they have open alarms too). If the camera isn't placed yet, it says so and where to place it. Opening an alarm while the Map page's top-down view is open also jumps that view to the camera. Placed cameras with an open alarm flash red on the top-down view.
- **Top-down site view** (map page → click a site → **Top-down view**): the 2D map switches to aerial imagery looking straight down and zooms into the site (up to z21; imagery is sharpest at z19). Placed cameras show as a dot, a name tag and a view cone on the ground; an open alarm from a camera turns its cone and tag red. **Click a camera** for its live window. Admins use **Place cameras** with the same editor as the 3D view (click the spot, aim with the mouse, then drag the white/orange/yellow handles or type Aim/Width/Distance). Placements are shared with the 3D view. **Satellite / Street**, **Re-center** (fits the site's cameras), **Exit top-down**. Plain Leaflet, no WebGL: works on any screen. Measured in dev: alarms on screen in 16-92 ms with the view open and live video playing, 44-61 ms while panning it. e2e: `tools/e2e/topdown.py`.
- **Dismiss** beside Real event / False alarm: in the critical pop-up, alarm details, the feed's Acknowledge dialog and Alarms-page bulk edit. Closes the alarm with no NX bookmark (an NX rule that forces acknowledgement is still cleared in NX). Shown as a "Dismissed" chip, with its own verdict filter. Bulk edit now handles more than 500 selected alarms.
- **Camera editing on the 3D view:** placing a camera is now click its spot, then aim with the mouse (the cone follows, direction and distance) and click. Placed cameras get drag handles: move (white), aim and distance (orange tip), width (yellow sides, any angle up to a 360° dome), plus Aim / Width / Distance number boxes. Changes save as soon as you let go. Click a camera on the map while **Place cameras** is open to edit it. The fixed 40 m distance and preset widths are gone.
- **Cameras on the 3D view** (migration 0012): admins click **Place cameras**, pick a camera from the site's NX list, click where it is, then click the direction it faces (or choose a 360° dome: one click). Each camera shows as a marker, a name tag and its view cone on the ground. **Click a camera** for a live window (the same relayed live video as the alarm drawer). An open alarm from a camera turns its cone and tag red. Placements are per site, audit-logged (`site.camera_placed`, `site.camera_removed`), and only placed cameras can be opened this way (`/media/sites/{id}/cameras/{device}/live.webm|jpg`).
- **"God's eye" HUD on the 3D view:** corner brackets, title, the selected site and its state, a UTC clock, site/camera/open-alarm counts, and the view centre in degrees-minutes-seconds with altitude and heading. Tactical monospace look in TWG orange.
- **Real 3D buildings when a Google key is set** (`GOOGLE_MAP_TILES_KEY`, a Map Tiles API browser key): Google Photorealistic 3D Tiles replace the flat aerial view; pins and cameras sit on the buildings and ground. Without the key, nothing changes. The map page now sends its origin (never the path) as Referer so a key restricted to the portal's addresses works.

### Fixed
- **Bounding boxes said "person · False":** the tag showed an unnamed attribute (CVEDIA's "Is smoking: False"). Tags now show only the kind: **Person**, **Vehicle** or **Animal**, whatever the plugin calls it (CVEDIA person, Axis Human/HumanFace, Car/Truck…). Same names on the timeline and in incident reports.
- The pinned camera live window fell apart (title squeezed beside the video, close button covered) whenever that camera had an open alarm: its "alarm" style clashed with the Alarms page's. 3D and top-down.
- Placing a camera for the first time cleared the "Saved ✓" confirmation at once (3D and top-down).
- An aerial tile that timed out upstream logged a server error (500 traceback); it's now a clean 502 and the map shows the coarser level.
- **A burst of loud alarms could stall new ones for 30 s or more:** building each alarm's clip held a database connection, and about 15 at once used them all, so new alarms couldn't be stored. Clips no longer hold one.
- **New alarms reached screens 2-3 s late while clips were converting:** video conversion now runs at the lowest CPU priority. Measured: critical pop-up in 92-178 ms with 500 open alarms and 4 conversions running.
- **A bulk acknowledgement froze other screens for 15 s or more** (each acknowledged alarm redrew the whole feed), holding back new alarms. Updates are now grouped; a new alarm is never held. The live feed shows the top 100 cards and "+N more".
- **The critical siren started up to 0.5 s after the pop-up** (it waited for the sound loop's next tick; measured 170-314 ms). It now plays on arrival: 33-42 ms. Repeats every 4 s, Silence and "a new critical breaks the silence" re-checked.

### Added (earlier)
- **3D site view** (map page → click a site → **3D view**): the view dives from space into a tilted 3D aerial view of the site, with every site's pin coloured by state (alarm, warning, offline, OK) and its open-alarm count. Drag to spin or tilt, click a pin to select that site, **Satellite / Street** imagery, **Re-center**, **2D map** to go back. Selecting a site or an alarm in the feed flies the 3D view there. The site list, site panel and live feed stay on screen.
  - Free, commercially usable imagery: **USGS The National Map** (public domain; 0.3 m NAIP aerials, US only at close range), proxied and cached by the portal like the street map (`/tiles/sat/…`, 30 days). The first visit to a site takes a few seconds to sharpen; later visits are served from the cache. No 3D buildings yet (they need a paid Google Maps key).
  - CesiumJS 1.146 (Apache-2.0) is bundled in `static/vendor/cesium` and downloaded only when someone opens the 3D view. Nothing goes to Cesium ion or any other third party.
  - **Alarms come first:** screens without graphics acceleration don't get the 3D view (measured on one: frames of 0.5 s, alarms 0.3-0.8 s late). On other screens, a watchdog times every frame and closes the view with a message if frames get slow (one over 1 s, or three over 150 ms within 10 s). A failed load leaves the 2D map. The view draws only when something changes. e2e: `tools/e2e/globe.py`.
- **Sign in with Google** (Platform page → Google sign-in; migration 0011): signs in **existing** accounts by their Google-verified email (nobody is created), optionally limited to email domains. Google's sign-in replaces the authenticator-app step; anyone required to have two-step sign-in with none set up is still walked through it, and password expiry still applies. Checks the state, PKCE and the ID token's issuer, audience, expiry and nonce. The page has click-level steps for the Google Cloud console and shows the exact redirect URI.
- **Email, invites and password resets** (migration 0010):
  - **Platform page → Email:** SMTP server, port, security (STARTTLS/SSL/none), username, password (encrypted, never shown again), From, and the portal address used in links. **Send test email**, the last success/error, and a list of recent emails (to, subject, result; never the content). Sends run in the background with 3 attempts. Click-level steps for a Google Workspace mailbox are on the page.
  - **Invite by email** is now the default when adding a user (Users page) or a company's first admin (Companies page). They get a TWG-branded email with a 7-day, single-use link to choose their own password, then two-step set-up if required. The admin always sees the link with a **Copy** button, so it works with email off too. Invited users show "Invited · email sent/failed" and get a **New invite link** button. The temporary-password option stays.
  - **Forgot password?** on the sign-in page: the answer is the same whether or not the account exists. The emailed link lasts 30 minutes, works once, and resetting signs the account out everywhere. At most 3 a hour per account; someone who never finished their invite gets a fresh invite instead.
  - **Password rules** (Platform page → Passwords): 12+ characters with upper and lower case, a number and a symbol by default, checked everywhere a password is set (users, companies, invites, resets, Account page, CLI). The error lists exactly what's missing. Optional **password expiry** (off by default): an expired password must be changed right after signing in, passkeys included.
- **Two-step sign-in** (migration 0009):
  - **Authenticator app** (Google/Microsoft Authenticator, 1Password…): set up on the new **Account** page (click your name, top right) with a QR code, confirmed with a code. 10 single-use **recovery codes** are shown once. Codes can't be replayed.
  - **Passkeys** (fingerprint, face, device PIN): add, test and remove them on the Account page. **"Sign in with a passkey"** needs no email, password or code. A passkey also works as the second step. They work at `https://alarmportal.twgsecurity.net` only (browsers refuse them on an IP address), so the LAN address keeps using the code.
  - **Who must use it** (Platform page → Two-step sign-in): TWG's own users, and customer companies either deciding for themselves (their admins: Settings → Sign-in security) or all required. Anyone required but not set up is walked through it at their next sign-in; nobody is locked out and nobody already signed in is signed out.
  - Admins see each user's two-step state on the Users page and can **reset** it (lost phone), optionally removing passkeys. CLI break-glass: `app.cli reset-mfa --email … [--passkeys]`.
- **Sessions:** every signed-in browser is listed on the Account page ("Where you're signed in") with a **Sign out** per browser and **Sign out everywhere**. Admins see and end their users' sessions. Changing or resetting a password signs out the other browsers.
  - **A signed-out screen is never quiet:** it keeps the page, turns the top bar to "Signed out", shows a red **SIGNED OUT, not receiving alarms** banner and sounds the connection tone every 10 s until someone signs in again. Measured: banner within 50 ms of an admin signing it out (e2e).
  - Browsers signed in before this release are adopted on the first request, so the upgrade signs nobody out.
  - Platform page → Sessions: how long a **closed** browser stays signed in (12 h, as before; an open page never goes idle), plus an optional maximum session length and keyboard/mouse idle sign-out, both **off** by default with a warning that they sign monitoring screens out too.
- **Sign-in protection** (Platform page → Sign-in protection; migration 0008):
  - Every sign-in attempt is recorded. **5 failures from one address in 10 minutes block it for 15 minutes**; each later block lasts 4× longer (1 h, 4 h, 16 h), capped at 7 days, and the 5th is permanent. A blocked address gets a "Temporarily blocked" page instead of the sign-in form.
  - **Account lock:** 10 failures for one email from any addresses in the window refuse that email (same "invalid" answer) until the window passes. Unknown emails take as long to answer as real ones.
  - **Never blocked:** addresses on our own network (so `https://10.1.10.97` always works), the tunnel connector, the allowlist, and any address a signed-in user was active from in the last 15 minutes. A block never signs anyone out or touches the live alarm stream.
  - **Real visitor addresses through the tunnel:** `CF-Connecting-IP` is believed only from the trusted connector (10.0.2.58), so the audit log stops showing the connector for everyone and a LAN user can't fake an address. The page flags an untrusted connector with a one-click "Trust".
  - Platform page: editable rules, your own address, blocked addresses (unblock, block by hand), the allowlist, locked accounts (unlock) and recent attempts (filter by address, email, result). Every change is audit-logged.
  - CLI break-glass: `unban`, `allow-ip`, `clear-lock`, `trust-proxy`.
- **Cloudflare edge bans** (Platform page → Cloudflare): with an API token and Zone ID, each block is also pushed to Cloudflare as an IP Access Rule and removed on unblock, allowlist or expiry. Best-effort: Cloudflare being down never weakens the portal's own block; errors show on the page and a sweep retries every minute. Only rules the portal made are ever removed.
- **Platform page** (`/platform`, TWG only; changes need `platform.manage`), linked from the top bar and the Companies page.

### Fixed
- **Users page:** an earlier edit had pasted the groups code inside the role/disable handler, so every role change or disable re-registered the group form's handlers (one Save could then submit several times). Rewritten. "+ New group" now waits until the user list and permissions have loaded (it could open an empty form).
- A signed-out page redirect now returns you to the page you were on after signing in.
- e2e `export` and `tenancy` read the audit page while it still said "Loading…" (the export flake noted on 2026-10-01). They now wait for the rows.
- **Backups**: `tools/backup.sh` saves the database dump, `.env` and checksums. Each run is verified by restoring into a scratch database and comparing row counts, and rotation keeps 14. Cron runs it nightly at 03:15 UTC.
- **Multiple companies:**
  - Other security companies get their own portal at the same address. They see only their own sites, alarms, users, groups, settings and audit log, with their name and logo ("Powered by TWG Security").
  - **TWG's views:** a top-bar company switcher with your own company, **All companies** (overview of every company's sites and alarms, labelled by company) or one company (**support mode**, with a banner).
  - **Support actions** need `platform.support` and are recorded in that company's audit log, marked "TWG support".
  - **Who hears what:** other companies' alarms never sound or pop up on TWG's screens, but TWG's own alarms keep sounding while TWG views another company.
  - **Companies page:** every company's sites (offline/disarmed), open alarms, users and last alarm. Create a company with its first admin, upload its logo, and turn its sign-in on or off. Turning sign-in off ends sessions, but its sites stay monitored.
  - Customer admins brand their own portal under Settings. Incident PDFs carry the company's logo.
  - New permissions `platform.view`, `platform.support` and `platform.manage`, which exist only in TWG.
  - `app.cli create-tenant` (migration 0007).
- **Follow-up notes** on any alarm, open or closed: "Notes" in the alarm drawer, with author and time, and Ctrl+Enter to add.
  - Append-only (never edited or deleted), audit-logged, and pushed live to other operators' drawers.
  - Included in the PDF report (Operator notes and timeline) and the evidence package manifest (migration 0006).
- **Verdicts:**
  - Every acknowledgement records the operator's call: **Real event** or **False alarm**. This applies in the drawer, the quick dialog and the critical pop-up, which now have two buttons instead of one Acknowledge.
  - The alarm list shows verdict chips and can be filtered by verdict (real / false / not marked).
  - The verdict is in the PDF report and the audit log. Pages opened before this change can still acknowledge without a verdict.
- **Groups & permissions** (Users page):
  - Admins create groups, tick permissions and pick members. The first permission is `alarms.bulk_edit`; admins always have it.
- **Bulk edit (admin override):**
  - On the Alarms page, select alarms (or "Select all shown") and mark them Real event or False alarm. Open ones are acknowledged with that verdict and the note; closed ones get their verdict changed.
  - The dialog spells out what will happen (counting critical alarms), and every change is audit-logged with old and new values.
  - Single overrides are available in the drawer.
- **Live beside recorded:**
  - The alarm drawer is wider: recorded clip on the left, **live video** on the right.
  - Live video is NX's WebM stream (VP8 640x360, ~0.8 Mbit/s, first frame ~1 s), relayed by the portal. MJPEG would have been about 17 times the bandwidth.
  - The live view stays near real time, pauses in hidden tabs, falls back to stills if the stream fails, and is capped at 6 streams per site and 20 min per stream.
  - The critical pop-up's Live button plays the same video.
- **Incident export**, from any alarm's details panel ("Export report / clip…"):
  - **Incident report (PDF)**, TWG-branded:
    - summary, with times in the site's time zone and UTC to the millisecond
    - the NX event-time frame, plus a sequence of stills from the clip (−5 s to +10 s) with analytics boxes drawn in
    - detected objects and their attributes
    - a timeline built from the audit log (event, arrival, the last arm/disarm before the event, acknowledgement and NX write-back, earlier exports)
    - the operator's disposition note, an optional note for the report, and the clip's SHA-256
  - **Evidence package (ZIP)**: the PDF, the MP4 clip, original and annotated stills, `manifest.json` and `SHA256SUMS.txt`.
  - Choice of clip window (default, ±30/60 s, ±60/120 s) and SD or HD.
  - Every export is audit-logged with its checksums. PDF building runs off the event loop; an alarm during an export arrived in 114 ms.
- **Site arming**:
  - Arm or disarm each site from the map's site panel or the Sites page.
  - A disarm can re-arm by itself: at the next scheduled arm, or after 30 min to 24 h. It can also be left until someone re-arms. The note is audit-logged.
  - Weekly **arming schedules** on the site form ("disarm 07:00 Mon–Fri, arm 18:00 Mon–Fri"), in the site's time zone and DST-aware. Saving a schedule never flips the state on the spot.
  - While a site is disarmed, security events are recorded ("While disarmed" filter on the Alarms page) but not raised. System alarms and NX rules tagged **`#24h`** still raise.
  - Disarmed sites show a DISARMED chip, a dashed map pin and a running "disarmed for" timer.
  - Scheduled and timer changes reach browsers live and are audit-logged (migration 0004).
- **NX rule health** on the map's site panel and the Sites page:
  - Alarm rules with NX's "Interval of action" set are flagged, because NX holds repeat events back and delivers them up to that interval late.
  - Also shown when the portal's NX account can't read rules at all.
- `tools/e2e/prod_probe.py`: measures alarm delivery over the LAN and the public (Cloudflare Tunnel) address side by side.

### Fixed
- The Sites page failed to load through Cloudflare after a deploy: Cloudflare turns `no-cache` into a 4-hour browser cache, so old and new scripts got mixed. Assets are now served from content-versioned URLs.
- Arming on the map's site panel is a clear card: ARMED or DISARMED, how long, who and why, when it changes next, and one full-width Arm or Disarm button.
- The public name `alarmportal.twgsecurity.net` showed a blank page: Caddy answered unknown hostnames with an empty 200. The name is now in the Caddyfile (`PUBLIC_HOST`).
- **Growing clips**: video starts about 7 s after an alarm (was about 30 s). The clip covers the footage recorded so far, is extended every 5 s in place, and is replaced by the full clip once recorded. Prefetch builds the first growing clip at +5 s.
- The portal logs the lag of every event NX pushes, which shows delays upstream of the portal.
- **Push delivery from NX**: a per-site JSON-RPC websocket (`rest.v4.events.log.subscribe`) gets alarms to the screen in about 0.1 s. Polling continues as a backstop, and a per-site lock serializes push and poll ingest.
- **No silent delays in the browser**:
  - auto-reconnect after any stream error, including the 502 that permanently killed EventSource during restarts
  - a 5 s heartbeat with a 12 s watchdog
  - polling open alarms every 2 s while the stream is down, with catch-up alarms raised with sound and pop-up
  - a red "LIVE UPDATES LOST" banner and a tone after 10 s
  - a "● Live" indicator in the top bar
- **Site connection lost** alarm when a site is unreachable for 60 s; the reconnect time is noted on it.
- Failed polls are retried after 1 s instead of a full cycle. Graceful shutdown is capped at 2 s, so restarts don't stall for 10 s.
- **Level per NX rule**: `#critical`, `#alarm`, `#warning` or `#ignore` in an NX rule's Title/Comment sets the level for that rule and overrides everything else. When several rules fire for one event, the loudest wins. The alarm details show where the level came from (`alarms.level_source`, migration 0003).
- **Alarm video clips**:
  - A looping clip around each alarm (default −10 s / +20 s) in the drawer and the critical pop-up.
  - Timeline with an alarm marker and object-presence marks; play/pause, frame step, speed, jump to alarm, widen earlier/later, HD, live and download.
  - NX's recorded start time keeps playback frame-accurate. Non-H.264 streams (H.265, MPEG-4 Part 2) are converted by ffmpeg.
  - Clips are cached on disk with range support, and pre-built for Critical and Alarm events.
- **Analytics bounding boxes** over the video, from NX object tracks and their per-frame metadata, in sync with playback.
- `tools/fake_nx.py` serves synthetic clips and moving object tracks.
- **Alarm levels**: Critical, Alarm, Warning or Ignore for each NX event type, set on an admin Settings page. Force-acknowledge NX rules are always Critical. Saving re-levels open alarms.
- **Audible alarms**:
  - Critical: a siren repeating every 4 s. Alarm: a chime repeating every 30 s. Warning: one tone.
  - "Silence 2 min" and a mute toggle.
  - A banner appears when the browser blocks sound.
  - Only one tab plays at a time.
- **Critical pop-up** on every page: site, address, camera, event-time frame, running timer, note and Acknowledge, Show on map, Silence, Minimize, and paging between several criticals.
- **Live alarm feed** on the overview page (right-hand column): level tabs with counts, a running "active for" timer on every card, and acknowledge or open details from the card.
- Soft-trigger alarms are named after the NX user who pressed them; events without a caption get readable labels.
- `tools/fake_nx.py` for end-to-end testing.

### Changed
- Maps moved from Google Maps to Leaflet + OpenStreetMap, so no API key is needed.
  - Tiles are darkened in dark mode.
  - Address search uses Nominatim through a rate-limited `/api/geocode` proxy.
  - The tile and geocoder URLs can be set in `.env`.

### Fixed
- Sites no longer flap offline on one-off relay 503s. A site shows offline only after 60 s of continuous failure; a bad password still shows at once.
- Map tiles were blocked by OpenStreetMap ("Access blocked"): browsers sent no Referer because of the portal's Referrer-Policy. Tiles are now proxied and cached server-side with an identifying User-Agent.
- Caddy `default_sni`, so HTTPS works when the portal is opened by IP address.

## [0.1.0] - 2026-09-29

### Added
- Base portal:
  - Map site view with alarm-aware pins
  - unified live alarm queue (SSE)
  - acknowledge with disposition notes and NX write-back (forced-ack clear or bookmark)
  - append-only audit log
- Sites are added through the vmsproxy relay (Nx Cloud ID) or a direct URL, with a Connect test. NX passwords are Fernet-encrypted at rest.
- Local accounts (argon2) with admin/operator roles, CSRF protection, and login throttling.
- Schema is tenant-scoped throughout (single "TWG Security" tenant seeded).
- Dark theme by default, plus a light theme, in TWG branding.
- Deployment with Docker Compose (app + Postgres 16 + Caddy HTTPS).

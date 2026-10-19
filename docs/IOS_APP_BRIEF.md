# TWG Alarm Portal: iOS app brief

For Claude Code on the Mac that builds the iOS app. Written 2026-10-16 from the portal's code (branch
`feature/base-portal`, commit `46b4a1a` and later). The portal itself is built and run on TWG's Linux server by a
separate Claude Code session; this app is a client of it.

Read the whole document before writing code. Where it says **(server: to build)**, that endpoint doesn't exist yet:
the Linux-side session builds it (the user carries messages between the two). Build against the contract
described here, with mocks until it's live.

---

## 1. What the portal is

TWG Security (The Wire Guys) is a commercial security integrator that monitors customer sites. The **TWG Alarm
Portal** watches NX Witness VMS servers (one per site, reached through Nx's cloud relay) and turns NX events into
alarms that operators see, verify on camera and close. It texts people (Twilio) and dispatches mobile guards.

- Production: `https://alarmportal.twgsecurity.net` (Cloudflare Tunnel, valid public certificate). Use this host.
  The LAN address `https://10.1.10.97` has a self-signed certificate: don't use it from the app.
- Companies (tenants): TWG plus customer companies. Every user belongs to one company; TWG staff with platform
  rights can see all companies. The server enforces all of this; the app just shows what the API returns.
- Roles: `admin` and `operator`, plus optional permissions from groups. Guards are ordinary users who are linked
  to a tracker (a GPS router in a vehicle, a Fleetio vehicle, or a phone) and are texted when dispatched.
- Source repo (read it for reference, don't change it): `github.com/TWG-Security/nx_alarm_portal`, branch
  `feature/base-portal`. `CLAUDE.md` there is the portal's own brief. Useful files: `app/routers/api.py`,
  `app/routers/guards.py`, `app/routers/links.py`, `app/routers/share.py`, `app/routers/stream.py`,
  `app/services/serialize.py` (the JSON shapes), `app/static/js/` (the web UI, the reference for behaviour).

## 2. Rules that are not negotiable

1. **An alarm must never be delayed or silently missed.** The app is an extra screen, never the only one.
   - Background iOS apps don't keep connections open, so alarms reach a phone by **push notification** (APNs).
     The live stream (section 6) is only for while the app is open and in front.
   - Text messages stay on as the backup. The app must not offer to turn them off "because I have the app".
   - If the app can't reach the server, or the live stream stops (no `ping` for 12 s), show a loud red banner
     ("Not receiving alarms") until it's back. Never fail quietly.
   - Measure: time from an NX event to the push on the phone, and to the alarm on screen with the app open.
     The web screen gets alarms in about 10–90 ms after the portal sees them.
2. **Real-world actions need a confirmation.** A verdict of Real event texts people; Dispatch texts guards. Ask
   "Are you sure?" with what will happen, as the web does.
3. **Respect the server.** Never cache another company's data, never store passwords. Keep the token in the
   Keychain (`kSecAttrAccessibleAfterFirstUnlock`, so push actions work while the phone is locked).
4. **Plain words in the UI.** Operators and guards aren't technical. "Alarm", "Real event", "False alarm",
   "Dismiss", "En route", "On scene", "Clear", as on the website.

## 3. Branding

| Token | Value | Use |
|---|---|---|
| Charcoal | `#1A1A1A` | text, dark backgrounds |
| White | `#FFFFFF` | light backgrounds |
| Red | `#C0392B` | critical / alarm state, dividers, heading accents |
| Red (text on dark) | `#EE7366` | red text on dark surfaces |
| Orange | `#E08A30` | buttons, calls to action (text on orange: `#1A1A1A`) |
| Green (ok) | `#2FA36B` | online, all clear |
| Amber (warning) | `#D9A21B` | warnings |
| Grey (offline) | `#767676` | offline |
| Dark surfaces | `#222222`, `#2C2C2C`, `#363636`; border `#3A3A3A`; muted text `#A8A8A8` |
| Light surfaces | `#F7F7F7`, `#EEEEEE`, `#E3E3E3`; border `#D6D6D6`; muted text `#5C5C5C` |

- Font: the system font (SF Pro) is fine; the web uses Arial/Helvetica/system sans-serif.
- Logo: `https://static.wixstatic.com/media/d4fc57_b28e915a1b924e33b12eb77028f74f62~mv2.png` (260×80). Bundle it.
- Dark mode first (control-room use), light mode supported. Alarm levels: critical = red, alarm = orange,
  warning = amber.

## 4. App scope (decided by the user: one app, screens by who signs in)

**Operators**
- Alarms list: open alarms, newest first, filter by level and site; badge counts per level.
- Push for new alarms by level (critical always; alarm and warning if the user chose them). Critical uses the
  Critical Alerts entitlement once Apple grants it (section 8), otherwise `time-sensitive`.
- Alarm details: caption, site and address, camera, time, the **picture at the alarm moment**, a **clip** of the
  seconds around it, **live view** of the camera, notes, and the timeline (texts sent and delivered, who viewed,
  guard status).
- Actions: **Real event / False alarm / Dismiss** with a note (closes the alarm; real texts people), add note,
  **Dispatch** guards (nearest first, with distance), **End call** for a dispatched alarm.
- Active calls: dispatched alarms still open, with each guard's latest status.
- Map (MapKit): site pins coloured by state, guards and vans with their last positions and trails.

**Guards**
- Push when dispatched, with the site and directions.
- Dispatch screen: alarm details (picture, clip, the site's live cameras), **En route / On scene / Clear**
  buttons, notes to dispatch, "Open in Apple Maps".
- Share location while on a call (foreground and background), so dispatch sees them move.

A person can be both (an operator who also gets dispatched): show both.

## 4a. Phase 1: a free Apple account (the user, 2026-10-16)

The user tests with a free Apple ID (Xcode "Personal Team") and joins the paid Apple Developer Program only when a
client wants the app. Build for that first:

- **No push notifications** (a free account can't use APNs), no Critical Alerts, no TestFlight. Skip section 6.2,
  section 8 and steps 4-5 of section 10 for now, but keep the code ready for them (a `PushService` that does nothing yet).
- Alarms reach the phone **only while the app is open**: the live stream (6.3). A new alarm plays a sound and shows
  an in-app banner (and a local notification if the app is just going to the background).
- **Monitoring mode** switch (Settings and the Alarms screen): keeps the screen awake (`isIdleTimerDisabled`) so a
  phone or iPad on a desk works like a wall screen.
- When the app goes to the background, show once: "Alarms reach this phone only while the app is open. Texts are
  still on." Never suggest turning texts off.
- The install runs on the user's own iPhone from Xcode and stops after 7 days (plug in and Run again). Up to 3 apps
  per free account at a time.
- Background location for guards on a call should work with a free account: check it on the device early.

## 5. What the Mac needs

- Xcode 16 or newer, Swift 6, **SwiftUI**, iOS 17+ minimum. No third-party dependencies unless they clearly earn
  it (URLSession, MapKit, AVKit, CoreLocation and UserNotifications cover everything).
- **Apple Developer Program** membership (paid; the user's TWG account). Needed for push, TestFlight and Critical Alerts.
- Bundle ID: **`com.twgsecurity.alarms`**; Apple Team ID: **`NY8H9358QD`** (the user's paid team, active 2026-10-19).
- Capabilities: Push Notifications; Background Modes → Remote notifications and Location updates; Time Sensitive
  Notifications; Critical Alerts (after Apple approves, section 8).
- Info.plist usage strings: `NSLocationWhenInUseUsageDescription`, `NSLocationAlwaysAndWhenInUseUsageDescription`
  ("So dispatch can see you while you're on a call. Only shared during a call."), `NSCameraUsageDescription` not needed.
- An **APNs auth key** (.p8) from the Apple Developer site, with its Key ID and the Team ID. The user gives these
  to the Linux side (they go on the portal's Platform page), never into the app.

## 6. The API (exists today unless marked)

Base URL `https://alarmportal.twgsecurity.net`. JSON in and out. Times are ISO 8601 UTC strings, except `*_ms`
and timeline `ts`, which are milliseconds since 1970. Errors: HTTP 4xx/5xx with `{"detail": "message for a person"}`
(show `detail` as is). `401` = signed out: go to sign-in. `403` = not allowed. `404` = not found or another company's.
`409` on acknowledge = someone else already closed it (refresh and say so).

### 6.1 Sign-in (live since 2026-10-19 14:17 UTC)

The website signs in with a cookie session, a CSRF token and HTML two-step pages, which doesn't suit an app.
The app signs in with these (`app/routers/mobile.py`; tests `tests/test_mobile.py`):

```
POST /api/mobile/login            {"email", "password", "device_name"}            (device_name: up to 300 chars, shown on
                                                                                    the website; needn't be unique)
  200 {"token": "twgm_…", "user": Me}                                  signed in
  200 {"step": "totp", "challenge": "…", "expires_in_s": 300[, "passkey": true]}   code, recovery code (or passkey: 6.1a)
  200 {"step": "passkey", "challenge": "…", "expires_in_s": 300, "passkey": true}  passkey only (6.1a)
  200 {"step": "enroll", "detail": "…"}                                two-step required but not set up: show detail
                                                                       (set it up on the website first)
  400 {"detail": "Your password has expired. Change it on the website …"}
  401 {"detail": "Invalid email or password."}                         wrong email or password, unknown, locked, deactivated
  403 {"detail": "This address is blocked after repeated failed sign-ins. Try again after … UTC."}   banned address
  403 {"detail": "Your company's portal access is disabled. Contact TWG Security."}
  422 FastAPI validation error (missing / empty field)

POST /api/mobile/login/totp       {"challenge", "code"}       code: 6 digits (spaces ignored) or a recovery code
  200 {"token": "twgm_…", "user": Me}
  400 {"detail": "That code didn't work."} or {"detail": "That code was already used. Wait for the next one."}  try again
  401 {"detail": "Too many wrong codes. Sign in again."}               after 5 wrong codes: back to email/password
  401 {"detail": "That took too long, or the code was already used. Sign in again."}   challenge expired (5 min) or used
  401 {"detail": "Sign in again."}                                     the account changed meanwhile
  403 banned address (as above)

POST /api/mobile/logout           (Bearer)  -> 200 {"signed_out": true}   ends this phone's token only
GET  /api/mobile/me               (Bearer)  -> Me
```

`Me` = `{id, email, display_name, role: "admin"|"operator", company: {id, name}, permissions: [strings],
is_guard: bool (a tracker is linked to them), monitoring_station: bool, sms_level: "off"|"critical"|"alarm",
sms_when: "raised"|"confirmed"|"both"}`.

- Every attempt is recorded like a website sign-in (failures lock the account and ban the address the same way).
- **Bearer:** every other call sends `Authorization: Bearer <token>`: all `/api/…` and `/media/…` endpoints and
  `GET /api/events`. No cookies, no CSRF. Send no cookies at all (`httpShouldHandleCookies = false`): a Bearer
  request ignores them anyway. `/tiles/…` (the website's map tiles) are not for the app; use MapKit.
- **Company:** a device token always sees the user's own company (no company switcher, for TWG staff too).
  `GET /api/alarms?state=open` already includes all of it: **don't send `alerting=1`** (harmless, not needed).
- **Lifetime:** no fixed expiry. A token ends when: the app signs out; it's revoked on the website (Account page →
  "Where you're signed in" lists the phone as "iPhone app (<device_name>)", admins see it under Users → Sign-in
  security); the user uses Sign out everywhere, changes their password, or an admin resets it or ends their
  sessions; the user is disabled or deleted; the company's sign-in is switched off; **30 days without being used**
  (or the platform's closed-browser limit if that's set longer); or the platform's "always sign out after" limit
  if one is set (monitoring station users are exempt). Then every call answers 401 and an open `/api/events` stream
  gets `event: signedout` (measured: 0.3 s after a revoke on the website).
- **Stream:** `retry: 3000`, then `event: ping` at once (measured 14 ms after connecting), then a ping every 5 s.
- A valid Bearer request counts as signed in, so the phone's address (Wi-Fi or cellular) is never banned while in use.

### 6.1a Passkeys and saved passwords in the app (built 2026-10-19)

- `GET https://alarmportal.twgsecurity.net/.well-known/apple-app-site-association` → 200, `application/json`, no
  redirect, no sign-in: `{"webcredentials": {"apps": ["NY8H9358QD.com.twgsecurity.alarms"]}}` (live since
  2026-10-19). From `APPLE_TEAM_ID` / `APPLE_BUNDLE_ID` in the server's `.env` (code default bundle id
  `com.twgsecurity.alarms`; with no valid Team ID the list is empty). Apple's CDN caches the file: after a change, delete and
  reinstall the app (or wait for Apple's refresh) so iOS reads the new one.
- `POST /api/mobile/login` two-step answers now say whether passkeys work as the second factor:
  `{"step": "totp", "challenge", "expires_in_s": 300, "passkey": true}` (code or passkey), and, new,
  `{"step": "passkey", "challenge", "expires_in_s": 300, "passkey": true}` when two-step is required and the person
  has passkeys but no authenticator app (this was a 400 before). `"passkey"` is absent when they have none.
- `POST /api/mobile/passkey/options` `{"device_name", "challenge"?}` → `{"challenge_id", "options", "expires_in_s": 300}`.
  `options` is the WebAuthn request (rpId, challenge, allowCredentials, userVerification "preferred", timeout).
  Without `challenge`: any passkey for the site (`allowCredentials: []`), i.e. sign in with no password. With the
  two-step `challenge`: only that person's passkeys. 401 if that challenge is gone (sign in again); 400 "This account
  has no passkeys…" / "Passkeys are turned off."; 403 banned address.
- `POST /api/mobile/passkey/verify` `{"challenge_id", "credential"}` (standard WebAuthn JSON, base64url) →
  `{"token", "user": Me}` like the password login. `challenge_id` is single use (5 min). 400 "That took too long. Try
  again." / "That passkey isn't registered here." / "The passkey couldn't be verified."; 403 banned address or "This
  account can't sign in…"; 400 "Your password has expired…" as for passwords. Every failure counts toward bans.

### 6.2 Push notifications (built 2026-10-19; switched on once Apple's key is installed on the server)

Server side: `app/apns.py`. They work like the alarm texts (`app/sms.py`): the same triggers, per-person choices,
flood limit and background sending. **Texts stay on as the backup; push never changes them.**

```
POST   /api/mobile/devices  (Bearer)  {"apns_token": "<hex, 32-200>", "environment": "sandbox"|"production",
                                       "app_version", "ios_version"}
       -> 200 {"registered": true, "push_ready", "push_level", "push_when"}
          upsert by token (case-insensitive), tied to THIS app sign-in; 400 with a website cookie (app token only)
DELETE /api/mobile/devices/{apns_token}  (Bearer)  -> 200 {"removed": true|false}   (only your own)
PUT    /api/mobile/me/push  (Bearer)  {"push_level"?, "push_when"?}  -> Me          (422 on other values)
```

- Register on every launch (tokens change). `environment`: `sandbox` for Xcode builds (development APNs), `production`
  for TestFlight / App Store. The server sends to api.sandbox.push.apple.com or api.push.apple.com per device.
- A device is dropped when its app sign-in ends (Sign out, revoked on the website, Sign out everywhere, 30 days unused),
  when the user is disabled or deleted, or when APNs answers 410 / BadDeviceToken / Unregistered / DeviceTokenNotForTopic.
- Settings (Me, Account page "App notifications (iPhone)", Users page): `push_level` "off" | "critical" | "alarm"
  (critical + alarm, **the default** until the person chooses) | "warning" (everything); `push_when` "raised" (default)
  | "confirmed" | "both"; which sites = the same as their texts (`sms_sites`). Me also has `"push_ready"` (the server
  has Apple's key).

What is pushed, all with `"sound": "default"`, apns-push-type `alert`, `apns-expiration` = now + 1 h:

| type | when (same as the text) | aps.alert | interruption-level | apns-collapse-id / priority | extra keys |
|---|---|---|---|---|---|
| `alarm` | a new alarm at their level | title `CRITICAL · <site>` / `ALARM · …` / `WARNING · …`, subtitle camera, body `<caption> · <time>` | time-sensitive (critical, alarm), active (warning) | `alarm-<id>` / 10 | `alarm_id`, `level`, `site_id` |
| `alarm` + `"summary": true` | past the flood limit (Platform → Text messages, per 10 min): one summary when the window has room | title `N more alarms`, body the sites | time-sensitive | `alarm-summary` / 10 | `count`, `alarm_id` (newest), `level`, `site_id` |
| `alarm` (title `CONFIRMED · <site>`) | Real event, to `confirmed`/`both` people (not whoever confirmed; guards who'll be auto-dispatched get `dispatch` instead) | body `<caption> · <time>[ · +N more] · by <name>` | time-sensitive | `alarm-<id>` / 10 | `confirmed_by` |
| `call_ended` (title `STAND DOWN · <site>`) | Real event changed to False alarm / Dismissed, to those pushed CONFIRMED | body `<caption>: False alarm (by …)` | time-sensitive | `alarm-<id>` / 10 | `alarm_id`, `verdict` |
| `dispatch` | they were dispatched | title `DISPATCH · <site>`, subtitle caption, body `<address>, <distance> away` | time-sensitive | `dispatch-<id>` / 10 | `alarm_id`, `link_token` (= the text's /a/ link), `level`, `site_id`, `auto` |
| `call_ended` | the call ended (End call, or False alarm / Dismiss), to dispatched guards who haven't tapped Clear | title `CALL ENDED · <site>` or `STAND DOWN · <site>` | time-sensitive | `dispatch-<id>` / 10 | `alarm_id` |
| `alarm_closed` | someone closed an alarm they were pushed (ack, bulk) | title `CLOSED · <site>`, body `<caption>: Real event / False alarm / Dismissed (by …)`, **no sound** | passive | `alarm-<id>` / 5 (replaces the alarm notification) | `alarm_id`, `verdict` |
| `test` | Account page → Send a test notification | `TWG Alarm Portal` / `Test notification…` | active | `test` | |

`thread-id` is `alarm-<id>` on all of them (`alarm-summary` for summaries). No `category` and no `mutable-content` yet:
tell the server when the app has categories / a Notification Service Extension, and they'll be added. Not built:
`guard_update` pushes, Critical Alerts (`"interruption-level": "critical"` needs the entitlement first).

Every push is logged and shows on the alarm's timeline ("App notification sent · to their iPhone (iOS …), accepted by
Apple", or "NOT sent" + Apple's reason). APNs only says it accepted a push; there is no delivery report.

Measured on the dev server (fake APNs): NX event → APNs request 34-89 ms (median 38 ms, 8 alarms); with APNs
hanging, alarms reached the operator screen in 41-53 ms and the stuck pushes gave up after ~21 s (logged as failed).

### 6.3 Live stream (open while the app is in front)

`GET /api/events` (Bearer) → `text/event-stream`. Read it with `URLSession.bytes(for:)` and parse lines
(`event: …`, `data: …`, blank line ends a message). The server sends `retry: 3000` first, then:

| event | data | do |
|---|---|---|
| `ping` | `{}` every 5 s | reset the 12 s watchdog; no ping for 12 s → reconnect and show "Reconnecting" |
| `alarm.new` | Alarm | add it, sound, banner |
| `alarm.updated`, `alarm.acked` | Alarm | replace it (closed ones leave the open list) |
| `alarm.note` | Note | add to that alarm's notes |
| `alarm.timeline` | `{alarm_id, guard: bool}` | re-read that alarm's timeline; `guard: true` = a guard update (chime on active calls) |
| `alarms.reload` | `{}` | re-read the open alarm list |
| `site.updated` | Site | replace it |
| `site.status` | `{site_id, status, detail?}` | update the site's online/offline |
| `site.push` | `{site_id, push: bool}` | live push to NX connected or not (informational) |
| `site.removed` | Site | remove |
| `tracker.moved` | Tracker | move the guard/van marker, extend its trail |
| `tracker.removed` | `{id}` | remove |
| `signedout` | `{}` | the session was ended elsewhere: sign out loudly |

After a reconnect, and every 60 s while connected, re-read `GET /api/alarms?state=open` (the web does the same:
belt and braces). `/api/events` takes the Bearer token (6.1).

### 6.4 Alarms

```
GET  /api/alarms?state=open|active|acknowledged|disarmed|all&site_id=&priority=1|2|3&verdict=real|false|dismissed|none
                &q=&before_id=&after_id=&order=newest|oldest&limit=1..500        -> [Alarm]
     state=open: waiting for an operator. state=active: dispatched calls still running.
GET  /api/alarms/{id}                                                          -> Alarm
POST /api/alarms/{id}/ack        {"note": "", "verdict": "real"|"false"|"dismissed"}   -> Alarm   (409: already closed)
POST /api/alarms/{id}/viewed                                                   -> {"logged": bool}  (call when details open)
GET  /api/alarms/{id}/notes                                                    -> [Note]
POST /api/alarms/{id}/notes      {"text": "…"}  (1-4000 chars)                 -> Note
GET  /api/alarms/{id}/timeline                                                 -> [TimelineRow]
POST /api/alarms/{id}/end-call   {"note": ""}                                  -> Alarm   (400 if not acknowledged yet)
GET  /api/summary                                                              -> {"open_critical", "open_alarm", "open_warning"}
```

`Alarm` (from `app/services/serialize.py alarm_dict`):

```
id, tenant_id, tenant_name, site_id, site_name, site_address,
level: "critical"|"alarm"|"warning", priority: 1|2|3, category: "security"|"system",
caption, description, source_name (camera name), device_id (camera id; "" = no camera), has_snapshot,
event_type, event_subtype, event_ts_ms, received_at,
state: "new" (open) | "acknowledged" | "disarmed",
acked_at, acked_by, ack_note, verdict: ""|"real"|"false"|"dismissed", verdict_by, verdict_at,
nx_ack_required, nx_ack_result, level_source,
call_state: ""|"active"|"ended", call_ended_at, call_ended_by, call_end_note
```

`Note` = `{id, alarm_id, text, by, at}`. `TimelineRow` = `{ts (ms), what, who, detail, kind: "event"|"operator"|"guard"|"text"|"system"}`.

### 6.5 Pictures and video

```
GET /media/alarms/{id}/snapshot.jpg       the frame at the alarm moment (JPEG; 404 if no camera; 502 if NX has none yet)
GET /media/alarms/{id}/live.jpg           the camera now (poll every 1-1.5 s for a live view)
GET /media/alarms/{id}/live.webm          live video, VP8/WebM: AVPlayer can't play it. Use live.jpg polling.
GET /api/alarms/{id}/clip?pre=&post=&quality=sd|hd
    -> {"status": "pending"|"processing"|"ready"|"error", "url": "/media/clips/….mp4", "message",
        "ready_in_s" (pending: footage not recorded yet), "partial", "final_in_s", ...}
       poll every ~1 s until "ready" (or show "message" on "error"), then play `url` with AVPlayer
       (H.264 MP4; the server transcodes anything else). A "partial" clip grows: ask again after final_in_s.
GET /media/sites/{site_id}/cameras/{device_id}/live.jpg     any camera of a site, live
GET /api/sites/{site_id}/cameras          -> [{device_id, name, in_nx, place}]
```

### 6.6 Sites, guards, vans, dispatch

```
GET  /api/sites                           -> [Site]  (id, tenant_name, name, address, lat, lng, enabled, status:
                                              "online"|"offline"|"auth_error"|…, status_detail, last_seen_at,
                                              camera_count, open_critical, open_alarm, open_warning, marker:
                                              "ok"|"warn"|"alarm"|"offline", arming {...}, timezone_effective)
GET  /api/trackers?trail_min=0..1440      -> [Tracker + "trail": [[lat, lng, iso], …]]
     Tracker = {id, tenant_id, name, kind: "guard"|"vehicle", source: "peplink"|"fleetio"|"phone", enabled,
                lat, lng, speed, heading, fix_at, checked_at, error, device_name, user_id, online, offline_since}
GET  /api/alarms/{id}/guards              -> {"guards": [{tracker_id, name, kind, source, person, user_id,
                                              distance_m, distance ("0.2 mi"), fix_at, online, usable, can_text,
                                              reason, closest}], "dispatched": [...], "from": "site"|…,
                                              "texts_ready": bool, "auto": bool}
POST /api/alarms/{id}/dispatch            {"tracker_ids": [ids]}  -> {"sent": [{to, person, distance, phone}],
                                              "failed": [{name, error}]}   (texts real people: confirm first)
```

### 6.7 Guard side (works today without signing in, by link token)

When a guard is dispatched they're texted a link `https://alarmportal.twgsecurity.net/a/<token>`. The token is the
key to that one alarm (expires after 24 h by default). The app gets the same token in the `dispatch` push
(`link_token`), so the guard screens can use these endpoints:

```
GET  /api/a/{token}                       -> {"alarm": Alarm (no nx_ack_result), "sent_to", "expires_at",
                                             "guard_status": ""|"en_route"|"on_scene"|"clear", "guard_notes": [Note],
                                             "lat", "lng" (the alarm camera's placed spot, else the site pin, else null),
                                             "position_from": "camera"|"site"|null}   (built 2026-10-19)
                                          404 unknown (counts as a failed sign-in), 410 expired. Logged as "Opened the
                                          alarm link" at most once per 5 minutes per person (re-reads aren't logged)
GET  /api/a/{token}/clip                  clip status (as 6.5)
GET  /media/a/{token}/snapshot.jpg | alarm.jpg (with analytics boxes) | live.jpg | clips/{name}.mp4
GET  /api/a/{token}/cameras               the site's other cameras -> /media/a/{token}/cameras/{device}/live.jpg
POST /api/a/{token}/status                {"status": "en_route"|"on_scene"|"clear"}
POST /api/a/{token}/note                  {"text": "…"} (1-2000 chars; at most 50 per person per alarm)
POST /api/a/{token}/share                 -> the guard's location-share token (makes them a phone tracker)
POST /api/g/{share_token}/pos             {"lat", "lng", "accuracy", "speed", "heading", "ts" (ms)}
                                          send at least every 10 s while sharing, sooner when moving;
                                          fixes vaguer than 250 m are ignored
POST /api/g/{share_token}/stop            stopped sharing
```

Guards list their current dispatches (built 2026-10-19):
`GET /api/mobile/dispatches` (Bearer) → `[{alarm_id, link_token, site_name, address, lat, lng, dispatched_at, status}]`:
dispatch links sent to me that haven't expired, on calls still active, newest first, one per alarm (the newest
link if dispatched twice). Only links from dispatches made after 2026-10-19's deploy are listed (older ones kept
only the token's hash).

Live, on the guard's own `/api/events` stream only (never anyone else's):
- `event: dispatch` `{alarm_id, link_token, site_name}` when they're dispatched
- `event: call_ended` `{alarm_id}` when a call they were dispatched to ends (End call, or False alarm / Dismiss)

Keep the 15 s re-read of `/api/mobile/dispatches` as the backstop after a reconnect.

## 7. Suggested structure

- `APIClient` (async/await, Bearer from Keychain, one place that maps 401 to sign-out and `detail` to errors).
- `EventStream` (the SSE reader with the 12 s watchdog and exponential reconnect, 1 s → 30 s cap).
- `AlarmStore` (`@Observable`): open alarms by id, applies stream events, re-syncs every 60 s.
- Screens: SignIn (+ code step), Alarms, AlarmDetail, ActiveCalls, Map, Dispatch (guard), Settings
  (push levels, sign out, server status, app version).
- `LocationSharer` (CoreLocation, `allowsBackgroundLocationUpdates` only while a dispatch is open, stop on Clear).
- Mocks: a `MockAPI` with sample JSON copied from section 6, so the UI can be built before the server parts exist.

## 8. Critical Alerts

Critical alerts sound even when the phone is muted or in Do Not Disturb. Apple grants the entitlement
(`com.apple.developer.usernotifications.critical-alerts`) on request, for apps like public safety and security
monitoring: request it at developer.apple.com/contact/request/notifications-critical-alerts-entitlement/ early (it
can take weeks). Until then, use `interruption-level: time-sensitive` and say so in Settings.

## 9. Testing

- The Linux side can fire real test alarms on TWG's own NX ("TWG Security Office") and on a fake NX (the repo's
  `tools/fake_nx.py`, used with `tools/dev_up.sh`: portal on :8099, login `admin@twgsecurity.com` /
  `Smoke-test-password-123!`, dev database only). The Mac can run that dev stack itself too: Python 3.12,
  `python -m venv .venv && .venv/bin/pip install -r requirements.txt`, an `.env.dev` like the Linux one (ask), then
  `tools/dev_up.sh`. Point the app at `http://<mac>:8099` for UI work (allow it in ATS for debug builds only).
- Use a separate test user on production for anything real; never test dispatch on real guards without warning them.
- Before calling it done: push arrives with the app killed, the phone locked, and in Low Power Mode; the red
  "Not receiving alarms" banner appears when Wi-Fi drops; a critical alarm while the app is open shows within a second.

## 10. Order of work

1. Mac: project, branding, mocks, Alarms list + Alarm detail against `MockAPI`.
2. Linux (server): 6.1 sign-in + Bearer on `/api` and `/media`, `/api/mobile/me`, `/api/events` with Bearer. **Done 2026-10-19.**
3. Mac: real sign-in, live stream, actions (verdict, note, end call, dispatch), pictures, clip, live view, map.
4. Linux: 6.2 device registration + APNs sending (needs the .p8 key, Key ID, Team ID, bundle ID).
5. Mac: push, categories, Notification Service Extension for the picture.
6. Linux: `/api/a/{token}` JSON and `/api/mobile/dispatches`. Mac: guard screens and location sharing.
7. TestFlight to a few operators and guards; measure push times; then the App Store (or keep it TestFlight/internal).

When something in this document doesn't match what the server answers, trust the server, note it, and tell the
user so the Linux side can fix the server or this document.

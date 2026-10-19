"""Text messages for alarms, through Twilio, each with a no-sign-in link to that alarm (app/alarm_links.py).

Alarm safety: poller._announce calls notify(), which never waits and never touches the database: with
texts off it returns at once, otherwise it starts a background task. The tasks take turns for the
database (DB_SLOTS, so a burst of hundreds of alarms can't empty the connection pool that ingest needs)
and never hold a connection while talking to Twilio (10 s timeout; 3 attempts on network errors, 429 and
5xx, 2 s and 6 s apart). A slow, failing or misconfigured Twilio never touches ingest or the live feed.

Who gets a text: active users of the alarm's own company with a mobile number, "Alarm texts" set to
critical (critical alarms only) or alarm (critical and alarm), who have opted in by replying YES, and
whose sites (users.sms_sites: None = every site, else a list of site ids) include the alarm's site. Warnings never text, and other companies'
alarms never text (the same rule as sound and pop-ups). Invited people who haven't signed in yet count:
someone can get texts without ever using the portal.

When (users.sms_when): "raised" texts when the alarm goes off (above), "confirmed" only once an operator marks
it Real event (a mobile guard: dispatch on a verified alarm), "both" does both. A confirmed text goes for an
alarm of any level (the operator's verdict is the signal; "No texts" still means none), names who confirmed
it and their note, and is never held back by flood control. Only alarms under CONFIRM_MAX_AGE_S old text
(re-marking old alarms in the Alarms page must not send guards out); a bulk edit sends each person one text
for the newest alarm with the count. The operator who confirmed isn't texted. If the verdict later changes
from Real event to something else, everyone who got the confirmed text gets a STAND DOWN text.

Flood control: at most sms_max_per_10min texts per person in any 10 minutes. Alarms past that are
counted, and one summary text ("12 more alarms at ...", with a link to the newest) goes out as soon as
the window allows. Nothing is dropped silently.

Consent (US carrier rules): adding a number sends one opt-in request ("Reply YES ..."); nothing else is
sent until they reply YES. Replies reach POST /twilio/sms (routers/links.py, Twilio-signed): YES/START opts
in (confirmation text), STOP and friends opt out (Twilio also blocks the number and sends its standard
confirmation), HELP gets the help text. Every alarm text ends "Reply STOP to opt out". Opt-ins and
opt-outs are audit-logged in the person's company. Public terms: /sms-terms.

Picture (MMS, Platform switch, on by default): the alarm frame with its analytics boxes (the incident
report's drawing), prepared once per alarm and cached, served to Twilio at /media/a/<token>/alarm.jpg.
Confirmed and Dispatch texts (sent after someone verified it) carry a GIF of the seconds around the alarm with
its boxes instead (services/alarm_gif.py, /media/a/<token>/alarm.gif), else the still.
GIF follow-up (the user, 2026-10-14: "send with a still image first once the gif is made send the gif"): the alarm
text itself still goes at once with the still (NX hasn't recorded the seconds after the alarm yet, and an alarm
text is never held back); once those seconds are recorded and the GIF is made, a second text "TWG ALARM VIDEO"
carries it to the same people (purpose alarm_gif). It goes only after their alarm text was accepted, not when
the GIF can't be made within GIF_TEXT_MAX_S, not once the alarm was marked False alarm / Dismissed, and not to
someone flood control is already holding back; it doesn't count toward their limit.
The text waits at most PICTURE_BUDGET_S for it; without a picture by then, or if Twilio refuses the
picture, the text goes out without one. A picture never costs the text.

Every attempt is written to sms_log (number, outcome, Twilio's message id and error, and the delay from
the alarm reaching the portal to Twilio accepting the text), never the link. The last success or error
shows on the Platform page.
"""

import asyncio
import logging
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app import alarm_links, platform_settings
from app.audit import audit
from app.config import get_settings
from app.db import sessionmaker
from app.models import Alarm, PlatformSettings, SmsLog, User, utcnow
from app.security import decrypt
from app.services import arming

log = logging.getLogger(__name__)

RETRY_DELAYS_S = (2, 6)
TIMEOUT_S = 10
WINDOW_S = 600
DB_SLOTS = 2                 # background tasks using the database at once (the pool is 5 + 10; ingest comes first)
SEND_SLOTS = 4               # Twilio requests at once
LEVELS = {"off": 0, "critical": 1, "alarm": 2}    # the loudest alarm priority that texts (1 critical, 2 alarm)
LEVEL_WORD = {1: "CRITICAL", 2: "ALARM", 3: "WARNING"}
WHEN = ("raised", "confirmed", "both")      # users.sms_when
CONFIRM_MAX_AGE_S = 3600     # a verdict on an older alarm sends no text


# ---------------------------------------------------------------- phone numbers
def normalize_phone(raw: str) -> str:
    """'' stays ''. US numbers may be typed any way ((570) 555-1234, 570.555.1234, 1-570-...);
    other countries need the + and country code. Raises ValueError with a readable message."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        number = "+" + digits
    elif len(digits) == 10:
        number = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        number = "+" + digits
    else:
        raise ValueError(f"'{raw}' isn't a mobile number. Use 10 digits for the US, or + and the country code.")
    if not re.fullmatch(r"\+[1-9]\d{7,14}", number) or (number.startswith("+1") and len(number) != 12):
        raise ValueError(f"'{raw}' isn't a mobile number. Use 10 digits for the US, or + and the country code.")
    return number


def masked(phone: str) -> str:
    """For logs: +1******1234."""
    return phone[:2] + "*" * (len(phone) - 6) + phone[-4:] if len(phone) > 6 else phone


# ---------------------------------------------------------------- settings
@dataclass(frozen=True)
class Twilio:
    sid: str
    token: str
    sender: str                 # +1... number, or a Messaging Service SID (MG...)
    link_hours: int
    max_per_window: int
    pictures: bool = True


async def config(db=None) -> Twilio | None:
    """The Twilio settings, or None when texts are off or incomplete."""
    if db is None:
        async with sessionmaker()() as s:
            row = await s.get(PlatformSettings, 1)
    else:
        row = await db.get(PlatformSettings, 1)
    if row is None or not row.sms_enabled or not (row.twilio_sid and row.twilio_token_enc and row.twilio_from):
        return None
    try:
        token = decrypt(row.twilio_token_enc)
    except Exception:  # noqa: BLE001 - FERNET_KEY changed
        log.error("sms: the Twilio auth token can't be decrypted; enter it again on the Platform page")
        return None
    return Twilio(row.twilio_sid, token, row.twilio_from, row.sms_link_hours, row.sms_max_per_10min, row.sms_pictures)


# ---------------------------------------------------------------- the words
def _ascii(s: str, limit: int) -> str:
    """Plain GSM-friendly text: one curly quote or emoji would switch the whole text to 70-character parts."""
    s = (s or "").replace("—", "-").replace("–", "-").replace("’", "'").replace("‘", "'")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3].rstrip() + "..."


def _clock(alarm: Alarm, at: datetime | None = None) -> str:
    """The event time (or `at`) in the site's time zone."""
    tz = arming.zone(alarm.site.timezone if alarm.site else None)
    t = (at if at is not None else datetime.fromtimestamp(alarm.event_ts_ms / 1000, timezone.utc)).astimezone(tz)
    return t.strftime("%-I:%M:%S %p %Z")


def directions_url(site) -> str:
    """A link phones open in their maps app with directions to the site (Google Maps URL, works on iPhone and Android)."""
    from urllib.parse import quote
    if site is None:
        return ""
    if site.lat is not None and site.lng is not None:
        dest = f"{site.lat:.5f},{site.lng:.5f}"
    elif (site.address or "").strip():
        dest = quote(" ".join(site.address.split()), safe=",")
    else:
        return ""
    # travelmode last: if a phone leaves the link's last character out (see alarm_links.page_url), only that
    # parameter breaks (Google ignores it), never the destination.
    return f"https://www.google.com/maps/dir/?api=1&destination={dest}&travelmode=driving"


VIDEO_LABEL = "VIDEO (alarm clip + live cameras):"
MAP_LABEL = "MAP (directions to the site):"


def _links(link: str, site) -> list[str]:
    """The two links, each under its own label with a blank line between, so a phone shows which is which.
    The video link comes first: it's the one to tap."""
    # A space after each link: an iPhone showed both links one character short (2026-10-13); the space is
    # what gets cut, and the links also end in characters they can lose (a "/", a spare parameter).
    out = ["", VIDEO_LABEL, link + " "]
    nav = directions_url(site)
    if nav:
        out += ["", MAP_LABEL, nav + " "]
    return out + ["", "Reply STOP to opt out."]


def alarm_text(alarm: Alarm, link: str) -> str:
    """One thing per line, easy to read on a lock screen:

    TWG ALARM: Panic button
    TWG Security Office
    123 Main St, Harrisburg, PA
    Front Door Cam, 2:03:22 PM EDT

    VIDEO (alarm clip + live cameras):
    https://alarmportal.twgsecurity.net/a/...

    MAP (directions to the site):
    https://www.google.com/maps/dir/?api=1&destination=40.26650,-76.88380

    Reply STOP to opt out.
    """
    site = alarm.site
    what = _ascii(alarm.caption or alarm.event_type, 60)
    cam = _ascii(alarm.source_name, 40)
    lines = [f"TWG {LEVEL_WORD.get(alarm.priority, 'ALARM')}: {what}",
             _ascii(site.name if site else "", 60),
             _ascii(site.address if site else "", 100),
             ", ".join(p for p in (cam if cam and cam.lower() not in what.lower() else "", _clock(alarm)) if p)]
    return "\n".join([line for line in lines if line] + _links(link, site))


def gif_text(alarm: Alarm) -> str:
    """The follow-up that carries the moving picture of an alarm texted a few seconds earlier:

    TWG ALARM VIDEO: Panic button
    TWG Security Office
    Front Door Cam, 2:03:22 PM EDT

    Reply STOP to opt out.
    """
    site = alarm.site
    what = _ascii(alarm.caption or alarm.event_type, 60)
    cam = _ascii(alarm.source_name, 40)
    lines = [f"TWG {LEVEL_WORD.get(alarm.priority, 'ALARM')} VIDEO: {what}",
             _ascii(site.name if site else "", 60),
             ", ".join(p for p in (cam if cam and cam.lower() not in what.lower() else "", _clock(alarm)) if p)]
    return "\n".join([line for line in lines if line] + ["", "Reply STOP to opt out."])


def confirmed_text(alarm: Alarm, link: str, by: str, more: int = 0) -> str:
    """An operator marked the alarm Real event. Like alarm_text, plus who confirmed it, when, and their note:

    TWG CONFIRMED: Panic button
    TWG Security Office
    123 Main St, Harrisburg, PA
    Front Door Cam, 2:03:22 PM EDT
    Confirmed real by Mike Miller, 2:04:10 PM EDT
    Note: man in black hoodie at the loading dock

    VIDEO (alarm clip + live cameras):
    https://alarmportal.twgsecurity.net/a/...

    MAP (directions to the site):
    https://www.google.com/maps/dir/?api=1&destination=40.26650,-76.88380

    Reply STOP to opt out.
    """
    lines = alarm_text(alarm, link).split("\n")
    lines[0] = lines[0].replace(f"TWG {LEVEL_WORD.get(alarm.priority, 'ALARM')}:", "TWG CONFIRMED:", 1)
    extra = [f"Confirmed real by {_ascii(by, 40) or 'an operator'}, {_clock(alarm, alarm.verdict_at or utcnow())}"]
    if more:
        extra.append(f"(+{more} more confirmed alarm{'s' if more != 1 else ''}, see the portal)")
    note = _ascii(alarm.ack_note, 120)
    if note:
        extra.append(f"Note: {note}")
    at = lines.index("")                  # the blank line before the links
    return "\n".join(lines[:at] + extra + lines[at:])


def distance_words(metres: float | None) -> str:
    """US units, as a guard would say it: "300 ft", "0.4 mi", "12 mi"."""
    if metres is None:
        return ""
    feet = metres * 3.28084
    if feet < 1000:
        return f"{max(10, round(feet / 10) * 10):.0f} ft"
    miles = metres / 1609.344
    return f"{miles:.1f} mi" if miles < 10 else f"{miles:.0f} mi"


SHARE_LABEL = "SHARE YOUR LOCATION (open, tap Start, keep it open):"


def dispatch_text(alarm: Alarm, link: str, by: str, distance_m: float | None, share_link: str = "", auto: bool = False) -> str:
    """An operator sends this guard (the closest one) to the alarm:

    TWG DISPATCH: Panic button
    TWG Security Office
    123 Main St, Harrisburg, PA
    Front Door Cam, 2:03:22 PM EDT
    Go now. Dispatched by Mike Miller, 2:04:10 PM EDT (you're 0.4 mi away)
    Note: man in black hoodie at the loading dock

    VIDEO (alarm clip + live cameras):
    https://...

    MAP (directions to the site):
    https://www.google.com/maps/dir/...

    SHARE YOUR LOCATION (open, tap Start, keep it open):     <- guards tracked by their phone's browser
    https://.../g/...

    Reply STOP to opt out.
    """
    lines = alarm_text(alarm, link).split("\n")
    lines[0] = lines[0].replace(f"TWG {LEVEL_WORD.get(alarm.priority, 'ALARM')}:", "TWG DISPATCH:", 1)
    away = distance_words(distance_m)
    how = "confirmed real" if auto else "dispatched"
    extra = [f"Go now. {how.capitalize()} by {_ascii(by, 40) or 'an operator'}, {_clock(alarm, utcnow())}"
             + (f" (you're {away} away)" if away else "")]
    note = _ascii(alarm.ack_note, 120)
    if note:
        extra.append(f"Note: {note}")
    at = lines.index("")
    lines = lines[:at] + extra + lines[at:]
    if share_link:
        stop = len(lines) - 2                      # before the blank line + "Reply STOP"
        lines = lines[:stop] + ["", SHARE_LABEL, share_link + " "] + lines[stop:]
    return "\n".join(lines)


def call_ended_text(alarm: Alarm, by: str, verdict: str = "") -> str:
    """End of a call, to the guards sent to it: CALL ENDED (End call) or STAND DOWN (marked false / dismissed)."""
    site = _ascii(alarm.site.name if alarm.site else "", 60)
    what = _ascii(alarm.caption or alarm.event_type, 60)
    who = _ascii(by, 40) or "Dispatch"
    if verdict:
        word = {"false": "a false alarm", "dismissed": "dismissed"}.get(verdict, verdict)
        return (f"TWG STAND DOWN: {what} at {site} was marked {word} by {who}, {_clock(alarm, utcnow())}. "
                f"You can clear.\nReply STOP to opt out.")
    note = _ascii(alarm.call_end_note, 120)
    return (f"TWG CALL ENDED: {what} at {site}. {who} ended the call, {_clock(alarm, utcnow())}. You can clear."
            + (f"\nNote: {note}" if note else "") + "\nReply STOP to opt out.")


def locate_text(company: str, by: str, url: str) -> str:
    """Dispatch wants to see where a guard is, without an alarm (map → Guards → Locate)."""
    return (f"TWG: {_ascii(by, 40) or 'Dispatch'} at {_ascii(company, 60)} asks where you are. Open the link, tap "
            f"Start sharing and keep the page open.\n\n{SHARE_LABEL}\n{url} \n\nReply STOP to opt out.")


def share_invite_text(company: str, url: str) -> str:
    return (f"TWG: {_ascii(company, 60)} dispatch asks you to share your location while you're on duty, "
            f"so the closest guard can be sent to an alarm.\n\n{SHARE_LABEL}\n{url} \n\nReply STOP to opt out.")


def stand_down_text(alarm: Alarm, verdict: str, by: str, more: int = 0) -> str:
    word = {"false": "a false alarm", "dismissed": "dismissed"}.get(verdict, "no longer confirmed")
    site = _ascii(alarm.site.name if alarm.site else "", 60)
    what = _ascii(alarm.caption or alarm.event_type, 60)
    also = f" (and {more} more)" if more else ""
    return (f"TWG STAND DOWN: {what} at {site}{also} is no longer confirmed real. "
            f"{_ascii(by, 40) or 'An operator'} marked it {word}, {_clock(alarm, alarm.verdict_at or utcnow())}.\n"
            f"Reply STOP to opt out.")


def summary_text(count: int, sites: list[str], since: Alarm, newest: Alarm, link: str) -> str:
    names = ", ".join(_ascii(s, 30) for s in sites[:3]) + (f" +{len(sites) - 3}" if len(sites) > 3 else "")
    return (f"TWG: {count} more alarm{'s' if count != 1 else ''} at {names} since {_clock(since)} "
            f"(too many to text one by one).\n\nNEWEST ALARM VIDEO:\n{link}")


PROGRAM = "TWG Security Alarm Portal"


def terms_url() -> str:
    return f"{platform_settings.current().portal_url}/sms-terms"


def optin_text(company: str, by: str) -> str:
    return (f"{PROGRAM}: {_ascii(by, 40)} added this number to get alarm alerts for {_ascii(company, 40)}. "
            f"Reply YES to sign up. Msg frequency varies. Msg & data rates may apply. "
            f"Reply HELP for help, STOP to opt out at any time. Terms: {terms_url()}")


def confirm_text(companies: list[str]) -> str:
    return (f"{PROGRAM}: you're signed up for alarm alerts for {_ascii(', '.join(companies), 80)}. "
            f"Msg frequency varies. Msg & data rates may apply. Reply HELP for help, STOP to opt out at any time.")


def help_text(contact: str) -> str:
    return (f"{PROGRAM} alarm alerts. Help: {_ascii(contact, 80) or 'twgsecurity.com'}. "
            f"Msg & data rates may apply. Reply STOP to opt out. Terms: {terms_url()}")


def test_text(by: str) -> str:
    return f"TWG Alarm Portal test text, sent by {_ascii(by, 60)}. Alarm texts will look like this, with a link to the alarm."


# ---------------------------------------------------------------- Twilio
async def _post(cfg: Twilio, to: str, body: str, media_url: str = "") -> tuple[int, dict]:
    base = get_settings().twilio_api_base.rstrip("/")
    data = {"To": to, "Body": body}
    if media_url:
        data["MediaUrl"] = media_url
    data["MessagingServiceSid" if cfg.sender.startswith("MG") else "From"] = cfg.sender
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.post(f"{base}/2010-04-01/Accounts/{cfg.sid}/Messages.json", data=data, auth=(cfg.sid, cfg.token))
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    return r.status_code, payload if isinstance(payload, dict) else {}


_send_slots: asyncio.Semaphore | None = None
_db_slots: asyncio.Semaphore | None = None


def _slots():
    global _send_slots, _db_slots
    if _send_slots is None:
        _send_slots, _db_slots = asyncio.Semaphore(SEND_SLOTS), asyncio.Semaphore(DB_SLOTS)
    return _send_slots, _db_slots


# Twilio errors about the number or the account: sending again without the picture wouldn't help.
NUMBER_ERRORS = ("20003", "21211", "21408", "21610", "21612", "21614", "30034")


async def deliver(cfg: Twilio, to: str, body: str, *, retry: bool = True, media_url: str = "") -> tuple[str, str, str, int]:
    """(outcome sent|failed, error, Twilio message SID, attempts). Retries network errors, 429 and 5xx;
    a 4xx (bad number, unverified trial number, wrong token) fails straight away, except that a 4xx
    with a picture attached is tried once more without the picture."""
    send_slots, _ = _slots()
    error, attempts = "", 0
    async with send_slots:
        for delay in (0, *RETRY_DELAYS_S) if retry else (0,):
            if delay:
                await asyncio.sleep(delay)
            attempts += 1
            try:
                status, payload = await _post(cfg, to, body, media_url)
            except httpx.HTTPError as e:
                error = f"Twilio unreachable: {type(e).__name__}: {e}"[:500]
                log.warning("sms to %s: attempt %d: %s", masked(to), attempts, error)
                continue
            if status in (200, 201):
                return "sent", "", str(payload.get("sid", ""))[:64], attempts
            error = f"Twilio {payload.get('code') or status}: {payload.get('message') or f'HTTP {status}'}"[:500]
            log.warning("sms to %s: attempt %d: %s", masked(to), attempts, error)
            if status != 429 and status < 500:
                if media_url and not any(c in error for c in NUMBER_ERRORS):
                    log.warning("sms to %s: sending again without the picture", masked(to))
                    media_url = ""
                    status, payload = 0, {}
                    try:
                        status, payload = await _post(cfg, to, body)
                    except httpx.HTTPError as e:
                        error = f"Twilio unreachable: {type(e).__name__}: {e}"[:500]
                    attempts += 1
                    if status in (200, 201):
                        return "sent", f"sent without the picture ({error})"[:500], str(payload.get("sid", ""))[:64], attempts
                break
    return "failed", error, "", attempts


async def _record(to: str, purpose: str, outcome: str, error: str, attempts: int, sid: str, *, alarm: Alarm | None = None,
                  user: User | None = None, tenant_id: int | None = None, delay_ms: int | None = None) -> int | None:
    _, db_slots = _slots()
    try:
        async with db_slots, sessionmaker()() as db:
            entry = SmsLog(to=to, purpose=purpose, outcome=outcome, error=error, attempts=attempts, twilio_sid=sid,
                           delay_ms=delay_ms, alarm_id=alarm.id if alarm else None,
                           tenant_id=tenant_id if tenant_id is not None else (alarm.tenant_id if alarm else None),
                           user_id=user.id if user else None)
            db.add(entry)
            row = await db.get(PlatformSettings, 1)
            if row is not None and outcome in ("sent", "failed"):
                if outcome == "sent":
                    row.sms_last_ok_at = utcnow()
                else:
                    row.sms_last_error, row.sms_last_error_at = f"{masked(to)}: {error}"[:500], utcnow()
            if entry.alarm_id is not None and entry.tenant_id is not None:
                from app.audit import timeline_changed
                timeline_changed(db, entry.tenant_id, entry.alarm_id)
            await db.commit()
            return entry.id
    except Exception as e:  # noqa: BLE001
        log.error("sms: could not write the send log: %s", e)
    return None


# ---------------------------------------------------------------- did it arrive? (Twilio delivery status)
# Twilio's 201 only means "accepted". Carriers can still refuse it, and an MMS whose picture Twilio can't
# download fails as a whole (63019: seen on production 2026-10-12, alarm 1880). So every text is followed
# up: its status is read back until it's final; a failed text that had a picture is sent again without it.
WATCH_AT_S = (3, 10, 25, 60, 120, 240)       # seconds after sending
FINAL = {"delivered", "undelivered", "failed", "read", "canceled"}
TWILIO_ERRORS = {
    "63019": "Twilio couldn't download the picture", "12300": "the picture's file type was refused",
    "11200": "the picture couldn't be fetched", "30019": "the picture is too big for the carrier",
    "30003": "the phone is off or unreachable", "30004": "the number blocks messages", "30005": "unknown or inactive number",
    "30006": "landline or a carrier that can't take texts", "30007": "blocked by the carrier as spam",
    "30008": "unknown carrier error", "30032": "the toll-free number isn't verified yet",
    "30034": "the number isn't registered for texting (A2P 10DLC)", "21610": "they replied STOP",
}
_watchers: set[asyncio.Task] = set()
WATCH = True                                  # tests switch the follow-up off unless they test it


def explain(code) -> str:
    code = str(code or "")
    return f"Twilio {code}: {TWILIO_ERRORS.get(code, 'see the Twilio console')}" if code else "no reason given"


async def _status(cfg: Twilio, message_sid: str) -> dict:
    base = get_settings().twilio_api_base.rstrip("/")
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.get(f"{base}/2010-04-01/Accounts/{cfg.sid}/Messages/{message_sid}.json", auth=(cfg.sid, cfg.token))
    return r.json() if r.status_code == 200 else {}


def _twilio_time(value) -> int | None:
    """Twilio's date_updated ("Wed, 14 Oct 2026 03:21:00 +0000") in ms, or None."""
    from email.utils import parsedate_to_datetime
    try:
        return int(parsedate_to_datetime(str(value)).timestamp() * 1000) if value else None
    except (TypeError, ValueError):
        return None


async def _set_outcome(log_id: int | None, outcome: str, error: str, to: str, at_ms: int | None = None) -> None:
    """The text's final status. A text about an alarm also gets an audit row "sms.delivery", so its timeline and the
    incident report show when it was sent AND when it was delivered (the user, 2026-10-14: "log when the message is
    sent and delivered"). at_ms: when Twilio says the status changed (else now; never before it was sent)."""
    if log_id is None:
        return
    _, db_slots = _slots()
    async with db_slots, sessionmaker()() as db:
        entry = await db.get(SmsLog, log_id)
        if entry is not None:
            entry.outcome = outcome
            entry.error = (f"{entry.error}; {error}" if entry.error and error else error or entry.error)[:500]
            if entry.alarm_id is not None and entry.tenant_id is not None:
                sent_ms = int(entry.ts.replace(tzinfo=entry.ts.tzinfo or timezone.utc).timestamp() * 1000)
                now = int(time.time() * 1000)
                at = at_ms if at_ms is not None and sent_ms - 2000 <= at_ms <= now + 5000 else now
                audit(db, entry.tenant_id, "sms.delivery", user_id=entry.user_id, alarm_id=entry.alarm_id, sms_id=entry.id,
                      purpose=entry.purpose, outcome=outcome if outcome != "sent" else "no_report", error=error,
                      to=masked(to), sent_ms=sent_ms, at_ms=max(at, sent_ms))
        if outcome in ("failed", "undelivered"):
            row = await db.get(PlatformSettings, 1)
            if row is not None:
                row.sms_last_error, row.sms_last_error_at = f"{masked(to)}: not delivered, {error}"[:500], utcnow()
        await db.commit()


async def _watch(cfg: Twilio, log_id: int | None, message_sid: str, user: User, body: str, purpose: str,
                 alarm: Alarm | None, media_url: str) -> None:
    waited = 0.0
    for at in WATCH_AT_S:
        await asyncio.sleep(at - waited)
        waited = at
        try:
            m = await _status(cfg, message_sid)
        except httpx.HTTPError:
            continue
        status = m.get("status") or ""
        if status not in FINAL:
            continue
        if status in ("delivered", "read"):
            await _set_outcome(log_id, "delivered", "", user.phone, _twilio_time(m.get("date_updated")))
            return
        why = explain(m.get("error_code"))
        log.error("sms %s for alarm %s to %s: %s (%s)", purpose, alarm.id if alarm else "-", masked(user.phone), status, why)
        await _set_outcome(log_id, status if status in ("failed", "undelivered") else "failed", why, user.phone,
                           _twilio_time(m.get("date_updated")))
        if str(m.get("error_code")) == "21610":
            await _mark_stopped(user.phone)
        elif purpose == "alarm_gif":
            pass                             # only the GIF follow-up sank: they already have the alarm text and still
        elif media_url.endswith("/alarm.gif") and alarm is not None and await alarm_picture(alarm) is not None:
            # The GIF sank it (too big for the carrier, ...): send it again with the still picture, now.
            log.warning("sms: alarm %s to %s: sending again with the still picture", alarm.id, masked(user.phone))
            await _send(cfg, user, body, purpose, alarm, time.time(), media_url[:-len("alarm.gif")] + "alarm.jpg",
                        note=f"resent with the still picture ({why})")
        elif media_url:
            # The picture sank the whole message: send the words alone, now.
            log.warning("sms: alarm %s to %s: sending again without the picture", alarm.id if alarm else "-", masked(user.phone))
            await _send(cfg, user, body, purpose, alarm, time.time(), "", note=f"resent without the picture ({why})")
        return
    await _set_outcome(log_id, "sent", "no delivery report from the carrier within 4 min", user.phone)


async def _mark_stopped(phone: str) -> None:
    """Twilio refused with 21610: the number replied STOP (perhaps before the portal heard about it)."""
    _, db_slots = _slots()
    async with db_slots, sessionmaker()() as db:
        for u in (await db.scalars(select(User).where(User.phone == phone, User.sms_consent != "stopped"))).all():
            u.sms_consent, u.sms_consent_at = "stopped", utcnow()
            audit(db, u.tenant_id, "sms.opted_out", target=u.email, phone=masked(phone), keyword="(Twilio 21610)")
        await db.commit()


async def _send(cfg: Twilio, user: User, body: str, purpose: str, alarm: Alarm | None, arrived: float,
                media_url: str = "", note: str = "") -> str:
    outcome, error, sid, attempts = await deliver(cfg, user.phone, body, media_url=media_url)
    if "without the picture" in error:
        media_url = ""                       # deliver() already dropped it
    error = "; ".join(x for x in (note, error) if x)
    if "Twilio 21610" in error:
        await _mark_stopped(user.phone)
    delay_ms = int((time.time() - arrived) * 1000)
    (log.info if outcome == "sent" else log.error)("sms %s for alarm %s to %s: %s after %d ms%s", purpose, alarm.id if alarm else "-",
                                                   masked(user.phone), outcome, delay_ms, f" ({error})" if error else "")
    log_id = await _record(user.phone, purpose, outcome, error, attempts, sid, alarm=alarm, user=user, tenant_id=user.tenant_id,
                           delay_ms=delay_ms if outcome == "sent" and alarm is not None else None)
    if outcome == "sent" and sid and WATCH:
        _spawn(_watch(cfg, log_id, sid, user, body, purpose, alarm, media_url), _watchers, limit_s=WATCH_AT_S[-1] + TASK_MAX_S)
    return outcome


GIF_TEXT_MAX_S = 30          # the GIF follow-up gives up if the GIF isn't made within this (nothing waits on it)
# One GIF text per camera per person in this long: an analytics burst (the user, 2026-10-14, two intrusions 3.4 s apart
# on one camera: "it sends two gifs i only want one") gets an alarm text per alarm but a single GIF. A follow-up that
# ends up not sent (no GIF, false alarm, ...) frees the camera for the next alarm's GIF.
GIF_ONE_PER_S = 60
_gif_claims: dict[tuple[int, str], tuple[int, float]] = {}   # (user id, camera or site) -> (alarm id, when claimed)


def _claim_gif(user: User, alarm: Alarm) -> bool:
    key, now = (user.id, alarm.device_id or f"site {alarm.site_id}"), time.time()
    held = _gif_claims.get(key)
    if held and now - held[1] < GIF_ONE_PER_S:
        log.info("sms: alarm %s: no GIF text to %s (alarm %s's GIF from the same camera %.0f s ago covers it)",
                 alarm.id, masked(user.phone), held[0], now - held[1])
        return False
    _gif_claims[key] = (alarm.id, now)
    for k in [k for k, (_, at) in _gif_claims.items() if now - at >= GIF_ONE_PER_S]:
        del _gif_claims[k]
    return True


def _release_gif(user: User, alarm: Alarm) -> None:
    key = (user.id, alarm.device_id or f"site {alarm.site_id}")
    if _gif_claims.get(key, (None,))[0] == alarm.id:
        del _gif_claims[key]


async def _alarm_then_gif(cfg: Twilio, user: User, body: str, alarm: Alarm, arrived: float, media: str,
                          token: str, gif_body: str) -> None:
    """The alarm text exactly as without a follow-up; once Twilio took it, the GIF follow-up is its own task."""
    if await _send(cfg, user, body, "alarm", alarm, arrived, media) == "sent" and _claim_gif(user, alarm):
        _spawn(_gif_after(cfg, user, alarm, token, gif_body), _gif_tasks, limit_s=GIF_TEXT_MAX_S + TASK_MAX_S)


async def _gif_after(cfg: Twilio, user: User, alarm: Alarm, token: str, body: str) -> None:
    sent = False
    try:
        sent = await _gif_follow_up(cfg, user, alarm, token, body)
    finally:
        if not sent:
            _release_gif(user, alarm)


async def _gif_follow_up(cfg: Twilio, user: User, alarm: Alarm, token: str, body: str) -> bool:
    from app.services import alarm_gif, clips
    wait = (alarm.event_ts_ms + alarm_gif.GIF_POST_MS + clips.PARTIAL_LAG_MS) / 1000 - time.time()
    if wait > 0:                         # until NX has recorded the seconds after the alarm: the full GIF, not a short one
        await asyncio.sleep(wait)
    if await alarm_gif.alarm_gif(alarm, budget_s=GIF_TEXT_MAX_S) is None:
        log.info("sms: alarm %s: no GIF, so no GIF text to %s (they have the still)", alarm.id, masked(user.phone))
        return False
    _, db_slots = _slots()
    async with db_slots, sessionmaker()() as db:
        verdict = await db.scalar(select(Alarm.verdict).where(Alarm.id == alarm.id))
    if verdict in ("false", "dismissed"):
        log.info("sms: alarm %s was marked %s: no GIF text to %s", alarm.id, verdict, masked(user.phone))
        return False
    if len(_sent.get(user.id, ())) >= cfg.max_per_window:
        log.info("sms: alarm %s: no GIF text to %s (flood control is holding their texts)", alarm.id, masked(user.phone))
        return False
    return await _send(cfg, user, body, "alarm_gif", alarm, time.time(), alarm_links.gif_url(token)) == "sent"


async def send_test(to: str, by: str, *, user: User | None = None) -> tuple[str, str]:
    cfg = await config()
    if cfg is None:
        return "disabled", "Text messages are switched off or not set up yet."
    outcome, error, sid, attempts = await deliver(cfg, to, test_text(by), retry=False)
    log_id = await _record(to, "test", outcome, error, attempts, sid, user=user, tenant_id=user.tenant_id if user else None)
    if outcome == "sent" and sid and WATCH:
        from types import SimpleNamespace
        _spawn(_watch(cfg, log_id, sid, user or SimpleNamespace(phone=to), "", "test", None, ""), _watchers,
               limit_s=WATCH_AT_S[-1] + TASK_MAX_S)
    return outcome, error


# ---------------------------------------------------------------- the picture (MMS)
PICTURE_BUDGET_S = 4.0       # the text waits at most this long for its picture
PICTURE_SLOTS = 3            # pictures being made at once (NX frame + tracks through the relay)
_pictures: dict[int, bytes] = {}
_PICTURES_MAX = 100
_picture_slots: asyncio.Semaphore | None = None


ARCHIVE_WAIT_S = 2.5         # how long the picture waits for NX to have the alarm-moment frame before using a live one


async def _build_picture(alarm: Alarm) -> bytes:
    """The frame at the alarm moment with its boxes. A camera that records on motion only may not have that
    frame yet when the text goes (NX answers 200 with 0 bytes; Twilio then fails the MMS with 63019): retry
    for ARCHIVE_WAIT_S, then use a live frame labelled LIVE, so the text still carries a real picture."""
    from app.routers.stream import camera_frame, snapshot_bytes
    from app.services import clips, report
    deadline = time.monotonic() + ARCHIVE_WAIT_S
    frame = None
    while frame is None:
        try:
            frame = await snapshot_bytes(alarm)      # raises on errors and on NX's empty "no recording" answer
        except Exception:  # noqa: BLE001
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.4)
    if frame is None:
        frame = await camera_frame(alarm.site, alarm.device_id, -1)
        log.info("sms: alarm %s: no recorded frame after %.1f s, the picture is a live frame", alarm.id, ARCHIVE_WAIT_S)
        return await asyncio.to_thread(_label_live, frame, f"LIVE {_clock(alarm, utcnow())}")
    try:
        objs = await clips.objects(alarm, alarm.event_ts_ms - 3000, alarm.event_ts_ms + 3000)
    except Exception:  # noqa: BLE001 - boxes are a bonus; the frame alone still helps
        objs = []
    return await asyncio.to_thread(report._annotate, frame, report._boxes_at(objs, alarm.event_ts_ms))


def _label_live(jpeg: bytes, label: str) -> bytes:
    """A red LIVE tag in the corner: this picture is from just after the alarm, not the alarm moment itself."""
    import io
    from PIL import Image, ImageDraw, ImageFont
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=max(14, img.width // 32))
    except TypeError:                     # Pillow < 10.1: fixed-size bitmap font
        font = ImageFont.load_default()
    x0, y0, x1, y1 = d.textbbox((8, 6), label, font=font)
    d.rectangle([x0 - 6, y0 - 4, x1 + 6, y1 + 4], fill=(192, 57, 43))
    d.text((8, 6), label, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


async def alarm_picture(alarm: Alarm, budget_s: float | None = None) -> bytes | None:
    """The alarm frame with its boxes (cached per alarm), or None if it isn't ready within budget_s."""
    budget_s = PICTURE_BUDGET_S if budget_s is None else budget_s
    if alarm.id in _pictures:
        return _pictures[alarm.id]
    if not alarm.device_id:
        return None
    global _picture_slots
    if _picture_slots is None:
        _picture_slots = asyncio.Semaphore(PICTURE_SLOTS)

    async def build():
        async with _picture_slots:
            return await _build_picture(alarm)
    try:
        pic = await asyncio.wait_for(build(), budget_s)
    except Exception as e:  # noqa: BLE001 - incl. timeout: the text goes without a picture
        log.warning("sms: no picture for alarm %s within %.0f s (%s)", alarm.id, budget_s, type(e).__name__)
        return None
    if not pic or pic[:2] != b"\xff\xd8":        # never hand Twilio an empty or broken picture
        log.warning("sms: alarm %s: the picture isn't a JPEG (%d bytes); the text goes without it", alarm.id, len(pic or b""))
        return None
    _pictures[alarm.id] = pic
    while len(_pictures) > _PICTURES_MAX:
        _pictures.pop(next(iter(_pictures)))
    return pic


async def verified_media(alarm: Alarm) -> str:
    """For a text sent after the alarm was verified (Confirmed, Dispatch): "gif" if the moving picture is ready
    within its budget, else "jpg" if the still is, else "". Both are made at once, so the wait is the longer
    budget (alarm_gif.GIF_BUDGET_S), not the sum."""
    from app.services import alarm_gif
    gif, still = await asyncio.gather(alarm_gif.alarm_gif(alarm), alarm_picture(alarm))
    return "gif" if gif is not None else "jpg" if still is not None else ""


def media_url(kind: str, token: str) -> str:
    return alarm_links.gif_url(token) if kind == "gif" else alarm_links.picture_url(token) if kind == "jpg" else ""


# ---------------------------------------------------------------- flood control
@dataclass
class _Held:
    count: int = 0
    first_id: int = 0
    newest_id: int = 0
    sites: list[str] = field(default_factory=list)
    flushing: bool = False


_sent: dict[int, deque] = {}          # user id -> monotonic times of texts in the last WINDOW_S
_held: dict[int, _Held] = {}


def _allow(user_id: int, limit: int) -> bool:
    q = _sent.setdefault(user_id, deque())
    now = time.monotonic()
    while q and now - q[0] >= WINDOW_S:
        q.popleft()
    if len(q) < max(1, limit):
        q.append(now)
        return True
    return False


def _wait_s(user_id: int) -> float:
    q = _sent.get(user_id)
    return max(0.0, WINDOW_S - (time.monotonic() - q[0])) if q else 0.0


async def _flush(user_id: int) -> None:
    """Send one summary for the alarms held back for this person, once the window has room."""
    held = _held[user_id]
    try:
        while True:
            await asyncio.sleep(_wait_s(user_id) + 0.05)
            cfg = await config()
            if cfg is None:
                return
            if _allow(user_id, cfg.max_per_window):
                break
        _held.pop(user_id, None)
        _, db_slots = _slots()
        async with db_slots, sessionmaker()() as db:
            user = await db.get(User, user_id)
            first, newest = await db.get(Alarm, held.first_id), await db.get(Alarm, held.newest_id)
            if user is None or not user.is_active or not user.phone or user.sms_consent != "yes" or newest is None:
                return
            token = await alarm_links.mint(db, newest, user, cfg.link_hours)
            await db.commit()
            body = summary_text(held.count, held.sites, first or newest, newest, alarm_links.url(token))
        await _send(cfg, user, body, "summary", newest, time.time())
    except Exception:  # noqa: BLE001
        log.exception("sms: summary for user %s failed", user_id)
    finally:
        held.flushing = False


# ---------------------------------------------------------------- alarms
_tasks: set[asyncio.Task] = set()
_flushers: set[asyncio.Task] = set()     # summaries waiting for the window (minutes): drain() doesn't wait for them
_gif_tasks: set[asyncio.Task] = set()    # GIF follow-ups (seconds after the alarm text): drain() doesn't wait for them
_started: dict[asyncio.Task, float] = {}
TASK_MAX_S = 120             # no text task runs longer (Twilio: 3 tries x 10 s + 8 s; picture: 4 s; DB writes)


def _spawn(coro, tasks: set = _tasks, limit_s: float | None = None) -> None:
    """Run in the background with a hard time limit: nothing can get stuck for good. Never raises."""
    limit = limit_s or TASK_MAX_S
    name = getattr(coro, "__qualname__", "sms task")

    async def guarded():
        try:
            await asyncio.wait_for(coro, limit)
        except asyncio.TimeoutError:
            log.error("sms: %s ran over %d s and was stopped", name, limit)
        except Exception:  # noqa: BLE001
            log.exception("sms: %s failed", name)
    try:
        task = asyncio.get_running_loop().create_task(guarded(), name=f"sms:{name}")
    except RuntimeError:
        coro.close()
        return
    tasks.add(task)
    _started[task] = time.monotonic()
    task.add_done_callback(tasks.discard)
    task.add_done_callback(lambda t: _started.pop(t, None))


def status() -> dict:
    """For the Platform page: texts being prepared or sent now, and summaries waiting for the window."""
    now = time.monotonic()
    ages = [now - _started[t] for t in _tasks if t in _started]
    return {"in_flight": len(_tasks), "summaries_waiting": len(_flushers), "checking_delivery": len(_watchers),
            "gif_texts_waiting": len(_gif_tasks),
            "oldest_s": round(max(ages), 1) if ages else 0, "max_s": TASK_MAX_S}


async def shutdown(grace_s: float = 2.0) -> None:
    """App shutdown: let texts in flight finish for grace_s, then cancel the rest (a restart never hangs on them)."""
    if _tasks:
        await asyncio.wait(list(_tasks), timeout=grace_s)
    pending = [t for t in [*_tasks, *_flushers, *_watchers, *_gif_tasks] if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        log.warning("sms: %d text task(s) cancelled at shutdown", len(pending))
        await asyncio.wait(pending, timeout=1.0)


def notify(alarms: list[Alarm]) -> None:
    """Called for every raised alarm (poller._announce). Never waits; does nothing while texts are off."""
    if not alarms or not platform_settings.current().sms_ready:
        return
    arrived = time.time()
    for a in alarms:
        if a.priority <= 2:
            _spawn(_handle(a.id, arrived))


async def _handle(alarm_id: int, arrived: float) -> None:
    try:
        _, db_slots = _slots()
        plan: list[tuple[User, str, str]] = []
        async with db_slots, sessionmaker()() as db:
            cfg = await config(db)
            alarm = await db.get(Alarm, alarm_id)
            if cfg is None or alarm is None or alarm.state != "new":
                return
            levels = [k for k, v in LEVELS.items() if v >= alarm.priority]
            users = (await db.scalars(select(User).where(User.tenant_id == alarm.tenant_id, User.is_active.is_(True),
                                                         User.phone != "", User.sms_level.in_(levels),
                                                         User.sms_when.in_(("raised", "both")),
                                                         User.sms_consent == "yes"))).all()
            users = [u for u in users if u.sms_sites is None or alarm.site_id in u.sms_sites]   # "Only these sites"
            for u in users:
                if _allow(u.id, cfg.max_per_window):
                    token = await alarm_links.mint(db, alarm, u, cfg.link_hours)
                    plan.append((u, alarm_text(alarm, alarm_links.url(token)), token))
                    continue
                held = _held.setdefault(u.id, _Held(first_id=alarm.id))
                held.count += 1
                held.newest_id = max(held.newest_id, alarm.id)       # alarms are handled concurrently: keep the newest
                held.first_id = min(held.first_id, alarm.id)
                if alarm.site and alarm.site.name not in held.sites:
                    held.sites.append(alarm.site.name)
                if not held.flushing:
                    held.flushing = True
                    _spawn(_flush(u.id), _flushers, limit_s=WINDOW_S + TASK_MAX_S)
                log.info("sms: alarm %s held back for %s (over %d texts in 10 min)", alarm.id, masked(u.phone),
                         cfg.max_per_window)
            gif_body = gif_text(alarm) if plan else ""
            await db.commit()
        has_picture = bool(plan) and cfg.pictures and await alarm_picture(alarm) is not None
        for u, body, token in plan:
            media = alarm_links.picture_url(token) if has_picture else ""
            if has_picture:                  # then the moving picture follows (gif_text)
                _spawn(_alarm_then_gif(cfg, u, body, alarm, arrived, media, token, gif_body))
            else:
                _spawn(_send(cfg, u, body, "alarm", alarm, arrived, media))
    except Exception:  # noqa: BLE001 - never let a text problem escape into the event loop's error log only
        log.exception("sms: alarm %s could not be texted", alarm_id)


# ---------------------------------------------------------------- confirmed by an operator
def notify_verdicts(confirmed: list[int], withdrawn: list[tuple[int, str]], by_id: int) -> None:
    """After an acknowledgement or verdict change is committed. confirmed: alarm ids that just became Real event;
    withdrawn: (alarm id, new verdict) that were Real event and aren't now. Never waits."""
    if not platform_settings.current().sms_ready:
        return
    arrived = time.time()
    if confirmed:
        _spawn(_handle_confirmed(list(confirmed), by_id, arrived))
    if withdrawn:
        _spawn(_handle_withdrawn(list(withdrawn), by_id))


def _recent(alarm: Alarm) -> bool:
    return alarm.event_ts_ms >= (time.time() - CONFIRM_MAX_AGE_S) * 1000


async def _handle_confirmed(alarm_ids: list[int], by_id: int, arrived: float) -> None:
    try:
        _, db_slots = _slots()
        plan: list[tuple[User, str, str, Alarm]] = []
        auto: list[tuple[int, int]] = []                 # (alarm id, tracker id) to dispatch
        async with db_slots, sessionmaker()() as db:
            cfg = await config(db)
            if cfg is None:
                return
            alarms = (await db.scalars(select(Alarm).where(Alarm.id.in_(alarm_ids), Alarm.verdict == "real"))).unique().all()
            old = [a.id for a in alarms if not _recent(a)]
            if old:
                log.info("sms: alarms %s confirmed but over %d s old: not texted", old, CONFIRM_MAX_AGE_S)
            alarms = [a for a in alarms if _recent(a)]
            by = await db.get(User, by_id)
            from app.models import Tenant, Tracker
            from app.services import dispatch
            for tenant_id in sorted({a.tenant_id for a in alarms}):
                group = [a for a in alarms if a.tenant_id == tenant_id]
                users = (await db.scalars(select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True),
                                                             User.phone != "", User.sms_level != "off",
                                                             User.sms_when.in_(("confirmed", "both")),
                                                             User.sms_consent == "yes"))).all()
                # Auto-dispatch: the guards among them (people linked to a tracked router or phone) aren't all texted;
                # only the closest one is sent (app/services/dispatch.py), even when that guard is the one who
                # confirmed it (the user, 2026-10-13: guards may confirm alarms themselves). Nobody with a usable
                # position: all get the confirmed text, as before. The confirmed text never goes to whoever confirmed.
                if dispatch.auto_on(await db.get(Tenant, tenant_id)):
                    linked = set((await db.scalars(select(Tracker.user_id).where(
                        Tracker.tenant_id == tenant_id, Tracker.enabled.is_(True), Tracker.user_id.is_not(None)))).all())
                    newest = max(group, key=lambda a: a.id)
                    guards = {u.id for u in users if u.id in linked and (u.sms_sites is None or newest.site_id in u.sms_sites)}
                    if guards:
                        pick = None if await dispatch.already(db, newest.id) else await dispatch.pick_closest(db, newest, guards)
                        if pick is not None or await dispatch.already(db, newest.id):
                            users = [u for u in users if u.id not in guards]
                        if pick is not None:
                            auto.append((newest.id, pick["tracker_id"]))
                for u in users:
                    if u.id == by_id:
                        continue
                    mine = [a for a in group if u.sms_sites is None or a.site_id in u.sms_sites]
                    if not mine:
                        continue
                    newest = max(mine, key=lambda a: a.id)
                    token = await alarm_links.mint(db, newest, u, cfg.link_hours)
                    _sent.setdefault(u.id, deque()).append(time.monotonic())   # counts toward the window, never held
                    plan.append((u, confirmed_text(newest, alarm_links.url(token), by.label if by else "", len(mine) - 1),
                                 token, newest))
            await db.commit()
        for alarm_id, tracker_id in auto:
            _spawn(_auto_dispatch(alarm_id, tracker_id, by))
        kinds = {}
        if plan and cfg.pictures:
            alarms_ = list({a.id: a for *_, a in plan}.values())
            for a, kind in zip(alarms_, await asyncio.gather(*(verified_media(a) for a in alarms_))):
                kinds[a.id] = kind
        for u, body, token, a in plan:
            _spawn(_send(cfg, u, body, "confirmed", a, arrived, media_url(kinds.get(a.id, ""), token)))
    except Exception:  # noqa: BLE001
        log.exception("sms: confirmed alarms %s could not be texted", alarm_ids)


async def _auto_dispatch(alarm_id: int, tracker_id: int, by: User | None) -> None:
    from app.services import dispatch
    try:
        await dispatch.dispatch(alarm_id, tracker_id, by, auto=True)
    except dispatch.DispatchError as e:
        log.error("sms: auto-dispatch for alarm %s failed: %s", alarm_id, e)


async def _handle_withdrawn(changes: list[tuple[int, str]], by_id: int) -> None:
    """Real event changed to false / dismissed: tell everyone who was texted that it was confirmed."""
    try:
        _, db_slots = _slots()
        plan: list[tuple[User, str, Alarm]] = []
        verdicts = dict(changes)
        async with db_slots, sessionmaker()() as db:
            cfg = await config(db)
            if cfg is None:
                return
            rows = (await db.execute(select(SmsLog.user_id, SmsLog.alarm_id).where(
                SmsLog.alarm_id.in_(list(verdicts)), SmsLog.purpose == "confirmed",
                SmsLog.outcome.in_(("sent", "delivered"))).distinct())).all()
            got: dict[int, list[int]] = {}
            for uid, aid in rows:
                if uid is not None:
                    got.setdefault(uid, []).append(aid)
            by = await db.get(User, by_id)
            for uid, aids in got.items():
                u = await db.get(User, uid)
                alarms = [a for a in [await db.get(Alarm, aid) for aid in sorted(set(aids))] if a and a.verdict != "real"]
                if u is None or not u.is_active or not u.phone or u.sms_consent != "yes" or not alarms:
                    continue
                newest = alarms[-1]
                plan.append((u, stand_down_text(newest, verdicts.get(newest.id, newest.verdict), by.label if by else "",
                                                len(alarms) - 1), newest))
        for u, body, a in plan:
            _spawn(_send(cfg, u, body, "stand_down", a, time.time()))
    except Exception:  # noqa: BLE001
        log.exception("sms: stand-down texts for %s failed", changes)


# ---------------------------------------------------------------- consent
async def request_opt_in(user_id: int, by: str) -> bool:
    """Text this person the opt-in request (background). False when texts are off. Marks them "pending"."""
    if not platform_settings.current().sms_ready:
        return False
    _spawn(_send_opt_in(user_id, by))
    return True


async def _send_opt_in(user_id: int, by: str) -> None:
    try:
        _, db_slots = _slots()
        async with db_slots, sessionmaker()() as db:
            cfg = await config(db)
            user = await db.get(User, user_id)
            if cfg is None or user is None or not user.phone or user.sms_consent in ("yes", "stopped"):
                return
            from app.models import Tenant
            tenant = await db.get(Tenant, user.tenant_id)
            user.sms_consent, user.sms_optin_sent_at = "pending", utcnow()
            await db.commit()
            body = optin_text(tenant.label if tenant else "your company", by)
        await _send(cfg, user, body, "optin", None, time.time())
    except Exception:  # noqa: BLE001
        log.exception("sms: opt-in request for user %s failed", user_id)


STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "REVOKE"}
START_WORDS = {"YES", "Y", "START", "UNSTOP", "SUBSCRIBE", "OPTIN"}
HELP_WORDS = {"HELP", "INFO"}


def keyword(body: str) -> str:
    words = re.findall(r"[A-Za-z]+", body or "")
    if not words:
        return ""
    first = words[0].upper()
    if first == "STOP" and len(words) > 1 and words[1].upper() == "ALL":
        return "STOPALL"
    return first


async def handle_reply(from_: str, body: str, message_sid: str) -> str:
    """A text someone sent to our number. Returns what it was: yes | stop | help | other | unknown (no such number)."""
    word = keyword(body)
    kind = "yes" if word in START_WORDS else "stop" if word in STOP_WORDS else "help" if word in HELP_WORDS else "other"
    try:
        phone = normalize_phone(from_)
    except ValueError:
        phone = ""
    from app.models import Tenant
    async with sessionmaker()() as db:
        cfg = await config(db)
        row = await db.get(PlatformSettings, 1)
        if row is not None:
            row.sms_last_inbound_at = utcnow()
        users = (await db.scalars(select(User).where(User.phone == phone))).all() if phone else []
        companies = []
        for u in users:
            if kind == "yes" and u.sms_consent != "yes":
                u.sms_consent, u.sms_consent_at = "yes", utcnow()
                audit(db, u.tenant_id, "sms.opted_in", target=u.email, phone=masked(phone), keyword=word, message=message_sid)
            elif kind == "stop" and u.sms_consent != "stopped":
                u.sms_consent, u.sms_consent_at = "stopped", utcnow()
                audit(db, u.tenant_id, "sms.opted_out", target=u.email, phone=masked(phone), keyword=word, message=message_sid)
            t = await db.get(Tenant, u.tenant_id)
            if t and t.label not in companies:
                companies.append(t.label)
        db.add(SmsLog(to=phone or (from_ or "")[:20], purpose="inbound", outcome=kind if users else f"{kind}?",
                      error="" if users else "no user has this number", twilio_sid=(message_sid or "")[:64],
                      user_id=users[0].id if len(users) == 1 else None,
                      tenant_id=users[0].tenant_id if len(users) == 1 else None))
        await db.commit()
        contact = row.sms_help_contact if row is not None else ""
    log.info("sms: reply from %s: %s (%d matching users)", masked(phone or from_ or "?"), kind, len(users))
    if cfg is not None and users and kind in ("yes", "help"):
        body_out = confirm_text(companies) if kind == "yes" else help_text(contact)
        _spawn(_send(cfg, users[0], body_out, "confirm" if kind == "yes" else "help", None, time.time()))
    elif cfg is not None and not users and kind == "help":
        _spawn(_reply_raw(cfg, phone, help_text(contact)))
    return kind if users else "unknown"


async def _reply_raw(cfg: Twilio, to: str, body: str) -> None:
    outcome, error, sid, attempts = await deliver(cfg, to, body)
    await _record(to, "help", outcome, error, attempts, sid)


def valid_signature(token: str, url: str, params: list[tuple[str, str]], signature: str) -> bool:
    """Twilio's X-Twilio-Signature: base64 HMAC-SHA1 of the URL plus every POST field (name+value, sorted)."""
    import base64
    import hashlib
    import hmac
    data = url + "".join(k + v for k, v in sorted(params))
    digest = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return bool(signature) and hmac.compare_digest(digest, signature)


async def drain() -> None:
    """Tests: wait for background texts (summaries waiting on the window are cancelled)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def reset() -> None:
    """Tests: forget flood-control state and per-loop semaphores."""
    global _send_slots, _db_slots
    for t in [*_tasks, *_flushers, *_watchers, *_gif_tasks]:
        t.cancel()
    _tasks.clear()
    _gif_tasks.clear()
    _gif_claims.clear()
    _flushers.clear()
    _watchers.clear()
    _started.clear()
    _sent.clear()
    _held.clear()
    _pictures.clear()
    _send_slots = _db_slots = None
    global _picture_slots
    _picture_slots = None
    from app.services import alarm_gif         # its caches are keyed by alarm id, and its semaphore is per event loop
    alarm_gif._gifs.clear(); alarm_gif._failed.clear(); alarm_gif._inflight.clear()
    alarm_gif._slots = None
    from app import apns
    apns.reset()

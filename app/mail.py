"""Email over SMTP (Platform page → Email): invites, password resets, the test email.

Ported from MCP-Control-Platform apps/api/src/mail/{mailer,templates}.ts. Sending never holds up a
request: send_later() runs in the background, 3 attempts (2 s and 6 s apart), 15 s timeout each.
Every send is written to email_log (to, subject, outcome; never the body) and the last success or error
shows on the Platform page. With email switched off, nothing is sent and the page shows the link to
pass on by hand instead.

Layout follows the TWG email rule: white background, a 5px #1A1A1A border around the header with the
logo enlarged and centred, #C0392B dividers and heading accent, #E08A30 button, Arial/Helvetica.
"""

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from markupsafe import Markup

from app import security_guard
from app.db import sessionmaker
from app.models import EmailLog, PlatformSettings, utcnow
from app.security import decrypt

log = logging.getLogger(__name__)

RETRY_DELAYS_S = (2, 6)
TIMEOUT_S = 15
FROM_NAME = "TWG Alarm Portal"


@dataclass(frozen=True)
class Smtp:
    host: str
    port: int
    tls: str             # starttls | ssl | none
    user: str
    password: str
    sender: str


async def config() -> Smtp | None:
    """The SMTP settings, or None when email is off or incomplete."""
    async with sessionmaker()() as db:
        row = await db.get(PlatformSettings, 1)
    if row is None or not row.smtp_enabled or not row.smtp_host or not row.smtp_from:
        return None
    try:
        password = decrypt(row.smtp_password_enc) if row.smtp_password_enc else ""
    except Exception:  # noqa: BLE001 - FERNET_KEY changed
        log.error("email: the SMTP password can't be decrypted; enter it again on the Platform page")
        return None
    return Smtp(row.smtp_host, row.smtp_port, row.smtp_tls, row.smtp_user, password, row.smtp_from)


def render(subject: str, heading: str, paragraphs: list[str], *, button: tuple[str, str] | None = None,
           footnote: str = "", preview: str = "") -> tuple[str, str]:
    """(html, plain text). paragraphs and footnote are plain text (escaped here)."""
    from app.deps import templates
    body = Markup("").join(Markup('<p style="margin:0 0 12px;">{}</p>').format(p) for p in paragraphs)
    html = templates.env.get_template("email/layout.html").render(
        subject=subject, heading=heading, body=body, footnote=footnote, preview=preview or paragraphs[0][:120],
        button={"label": button[0], "url": button[1]} if button else None)
    text = "\n\n".join([heading, *paragraphs, *([f"{button[0]}: {button[1]}"] if button else []), *([footnote] if footnote else []),
                        "-- \nTWG Security Alarm Portal"])
    return html, text


def _deliver(cfg: Smtp, msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if cfg.tls == "ssl":
        server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=TIMEOUT_S, context=ctx)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=TIMEOUT_S)
    try:
        server.ehlo()
        if cfg.tls == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if cfg.user:
            server.login(cfg.user, cfg.password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass


async def _status(ok: bool, error: str = "") -> None:
    try:
        async with sessionmaker()() as db:
            row = await db.get(PlatformSettings, 1)
            if row is not None:
                if ok:
                    row.smtp_last_ok_at, row.smtp_last_error, row.smtp_last_error_at = utcnow(), "", None
                else:
                    row.smtp_last_error, row.smtp_last_error_at = error[:500], utcnow()
                await db.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("email: could not store status: %s", e)


async def send_now(to: str, subject: str, html: str, text: str, *, purpose: str, tenant_id: int | None = None,
                   user_id: int | None = None, retry: bool = True) -> tuple[str, str]:
    """Send (with retries). Returns (outcome, error): outcome is sent | failed | disabled."""
    cfg = await config()
    outcome, error, attempts = "disabled", "", 0
    if cfg is not None:
        msg = EmailMessage()
        name, addr = parseaddr(cfg.sender)
        msg["From"] = formataddr((name or FROM_NAME, addr or cfg.sender))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=(addr or cfg.sender).split("@")[-1] or None)
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        delays = (0, *RETRY_DELAYS_S) if retry else (0,)
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            attempts += 1
            try:
                await asyncio.to_thread(_deliver, cfg, msg)
                outcome, error = "sent", ""
                break
            except Exception as e:  # noqa: BLE001 - any SMTP/network failure: retry, then report
                outcome, error = "failed", f"{type(e).__name__}: {e}"[:500]
                log.warning("email %s to %s: attempt %d failed: %s", purpose, to, attempts, error)
        await _status(outcome == "sent", f"{subject} to {to}: {error}" if error else "")
    log.info("email %s to %s: %s", purpose, to, outcome)
    try:
        async with sessionmaker()() as db:
            db.add(EmailLog(to=to[:320], subject=subject[:300], purpose=purpose, outcome=outcome, error=error,
                            attempts=attempts, tenant_id=tenant_id, user_id=user_id))
            await db.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("email: could not write the send log: %s", e)
    return outcome, error


def send_later(*args, **kwargs) -> None:
    """send_now() in the background (the request answers straight away)."""
    security_guard.spawn(send_now(*args, **kwargs))


async def drain() -> None:
    await security_guard.drain()


# ---------------------------------------------------------------- the messages
def invite(email: str, company: str, url: str, invited_by: str) -> tuple[str, str, str]:
    subject = f"You're invited to the {company} alarm portal"
    html, text = render(subject, "You're invited", [
        f"{invited_by} has given you an account on the {company} alarm portal"
        + ("." if company == "TWG Security" else ", run by TWG Security."),
        f"Your sign-in is {email}. Choose your password to finish setting it up.",
    ], button=("Set up your account", url), footnote="The link works once and expires in 7 days.")
    return subject, html, text


def reset(url: str, minutes: int = 30) -> tuple[str, str, str]:
    subject = "Reset your alarm portal password"
    html, text = render(subject, "Reset your password", [
        "Someone (hopefully you) asked to reset the password for this alarm portal account.",
        "If it wasn't you, ignore this email: your password stays as it is.",
    ], button=("Choose a new password", url),
        footnote=f"The link works once and expires in {minutes} minutes. Resetting signs this account out everywhere.")
    return subject, html, text


def test_message(by: str) -> tuple[str, str, str]:
    subject = "Alarm portal test email"
    html, text = render(subject, "Email works", [
        f"{by} sent this test from the Platform page. Invites and password resets will arrive like this.",
    ])
    return subject, html, text


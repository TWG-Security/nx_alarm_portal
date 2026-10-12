"""Invite a user by email: a 7-day setup link, emailed when email is on, and always returned to the admin
so it can be passed on by hand (the Users and Companies pages show it with a Copy button)."""

from sqlalchemy.ext.asyncio import AsyncSession

from app import mail, tokens
from app.models import Tenant, User


async def invite(db: AsyncSession, user: User, tenant: Tenant, by: User) -> dict:
    """Mint the link (the caller commits); the email goes out in the background after commit."""
    token, row = await tokens.mint(db, user, "setup", by=by.id)
    url = tokens.link("setup", token)
    email_on = await mail.config() is not None
    if email_on:
        subject, html, text = mail.invite(user.email, tenant.label, url, by.label)
        from app.security_guard import after_commit

        async def _send(_ip):
            await mail.send_now(user.email, subject, html, text, purpose="invite", tenant_id=tenant.id, user_id=user.id)
        after_commit(db, _send, "")
    return {"setup_link": url, "email": "sending" if email_on else "off",
            "expires_at": row.expires_at.isoformat()}

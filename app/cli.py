"""Admin CLI.

    python -m app.cli create-admin --email you@twgsecurity.com --name "Your Name"
    python -m app.cli create-tenant --name "Acme Security" --admin-email ops@acme.example --admin-name "Ops Lead"
    (both prompt for the password; or pipe it in with --password-stdin)
    python -m app.cli unban --ip 203.0.113.7               lift a sign-in ban (and its Cloudflare rule)
    python -m app.cli allow-ip --ip 204.186.88.58 --label "TWG office"   never ban this address or range
    python -m app.cli clear-lock --email someone@example.com   unlock an account locked by failed sign-ins
    python -m app.cli trust-proxy --ip 10.0.2.58           believe CF-Connecting-IP from this tunnel connector
    python -m app.cli reset-mfa --email someone@example.com [--passkeys]   turn off their two-step sign-in
The first company created becomes the platform (TWG Security); later ones are customer companies.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.db import sessionmaker
from app.models import Tenant, User
from app.security import hash_password

DEFAULT_TENANT = "TWG Security"


async def create_admin(email: str, name: str, password: str, tenant_name: str) -> None:
    async with sessionmaker()() as db:
        tenant = await db.scalar(select(Tenant).where(Tenant.name == tenant_name))
        if tenant is None:
            has_platform = await db.scalar(select(Tenant.id).where(Tenant.kind == "platform"))
            tenant = Tenant(name=tenant_name, kind="customer" if has_platform else "platform")
            db.add(tenant)
            await db.flush()
        existing = await db.scalar(select(User).where(User.email == email.lower()))
        if existing:
            existing.password_hash, existing.role, existing.is_active = hash_password(password), "admin", True
            print(f"Updated existing user {email} -> admin, password reset")
        else:
            db.add(User(tenant_id=tenant.id, email=email.lower(), display_name=name,
                        password_hash=hash_password(password), role="admin"))
            print(f"Created admin {email} in tenant '{tenant_name}'")
        await db.commit()


async def create_tenant(name: str, admin_email: str, admin_name: str, password: str) -> None:
    async with sessionmaker()() as db:
        if await db.scalar(select(Tenant).where(Tenant.name == name)):
            sys.exit(f"A company named '{name}' already exists")
        if await db.scalar(select(User).where(User.email == admin_email.lower())):
            sys.exit(f"A user {admin_email} already exists")
        tenant = Tenant(name=name, kind="customer")
        db.add(tenant)
        await db.flush()
        db.add(User(tenant_id=tenant.id, email=admin_email.lower(), display_name=admin_name,
                    password_hash=hash_password(password), role="admin"))
        await db.commit()
        print(f"Created company '{name}' with admin {admin_email}")


async def unban_ip(ip: str) -> None:
    from app import platform_settings
    from app import security_guard as guard
    await platform_settings.refresh()
    async with sessionmaker()() as db:
        active = await guard.unban(db, ip, by="CLI")
        await db.commit()
    await guard.drain()                                    # Cloudflare removal; the app's sweeper retries if it failed
    print(f"Unbanned {ip}" if active else f"{ip} wasn't banned; its earlier failures no longer count")


async def allow_ip(ip: str, label: str) -> None:
    from app import platform_settings
    from app import security_guard as guard
    await platform_settings.refresh()
    async with sessionmaker()() as db:
        try:
            entry = await guard.allow(db, ip, label, by="CLI")
        except ValueError as e:
            sys.exit(str(e))
        await db.commit()
    await guard.drain()
    print(f"Allowlisted {entry.ip}" + (f" ({entry.label})" if entry.label else "") + ". The app picks it up within 30 s.")


async def clear_lock(email: str) -> None:
    from app import security_guard as guard
    async with sessionmaker()() as db:
        await guard.clear_lock(db, email, by="CLI")
        await db.commit()
    print(f"Cleared the sign-in lock on {email.strip().lower()}")


async def trust_proxy(ip: str) -> None:
    from app import net, platform_settings
    try:
        nets = platform_settings.parse_networks(ip)
    except ValueError as e:
        sys.exit(str(e))
    if not all(net.is_internal(n.network_address) and net.is_internal(n.broadcast_address) for n in nets):
        sys.exit("Only private addresses (the connector on your own network) can be trusted")
    async with sessionmaker()() as db:
        row = await platform_settings.get_row(db)
        have = platform_settings.parse_networks(row.trusted_proxies or "")
        merged = have + [n for n in nets if n not in have]
        row.trusted_proxies = ", ".join(str(n.network_address) if n.num_addresses == 1 else str(n) for n in merged)
        await db.commit()
        print(f"Trusted proxies: {row.trusted_proxies}. The app picks it up within 30 s.")


async def reset_mfa(email: str, passkeys: bool) -> None:
    from sqlalchemy import delete, func
    from app import mfa
    from app.audit import audit
    from app.models import WebAuthnCredential
    async with sessionmaker()() as db:
        user = await db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
        if user is None:
            sys.exit(f"No user {email}")
        await mfa.disable_totp(db, user)
        if passkeys:
            await db.execute(delete(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id))
        audit(db, user.tenant_id, "mfa.reset", target=user.email, via="CLI", passkeys_removed=passkeys)
        await db.commit()
    print(f"Two-step sign-in reset for {user.email}" + (" (passkeys removed too)" if passkeys else ""))


def main() -> None:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("create-admin")
    ca.add_argument("--email", required=True)
    ca.add_argument("--name", default="")
    ca.add_argument("--tenant", default=DEFAULT_TENANT)
    ca.add_argument("--password-stdin", action="store_true")
    ct = sub.add_parser("create-tenant")
    ct.add_argument("--name", required=True)
    ct.add_argument("--admin-email", required=True)
    ct.add_argument("--admin-name", default="")
    ct.add_argument("--password-stdin", action="store_true")
    ub = sub.add_parser("unban")
    ub.add_argument("--ip", required=True)
    al = sub.add_parser("allow-ip")
    al.add_argument("--ip", required=True)
    al.add_argument("--label", default="")
    cl = sub.add_parser("clear-lock")
    cl.add_argument("--email", required=True)
    rm = sub.add_parser("reset-mfa")
    rm.add_argument("--email", required=True)
    rm.add_argument("--passkeys", action="store_true", help="also remove their passkeys")
    tp = sub.add_parser("trust-proxy")
    tp.add_argument("--ip", required=True)
    args = p.parse_args()

    if args.cmd == "reset-mfa":
        asyncio.run(reset_mfa(args.email, args.passkeys))
        return
    if args.cmd == "trust-proxy":
        asyncio.run(trust_proxy(args.ip.strip()))
        return

    if args.cmd == "unban":
        asyncio.run(unban_ip(args.ip.strip()))
        return
    if args.cmd == "allow-ip":
        asyncio.run(allow_ip(args.ip.strip(), args.label))
        return
    if args.cmd == "clear-lock":
        asyncio.run(clear_lock(args.email))
        return

    if args.cmd == "create-tenant":
        password = sys.stdin.readline().rstrip("\n") if args.password_stdin else getpass.getpass("Admin password: ")
        if len(password) < 12:
            sys.exit("Password must be at least 12 characters")
        asyncio.run(create_tenant(args.name, args.admin_email, args.admin_name, password))
        return
    if args.cmd == "create-admin":
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Confirm:  "):
                sys.exit("Passwords do not match")
        if len(password) < 12:
            sys.exit("Password must be at least 12 characters")
        asyncio.run(create_admin(args.email, args.name, password, args.tenant))


if __name__ == "__main__":
    main()

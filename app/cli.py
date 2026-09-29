"""Admin CLI.

    python -m app.cli create-admin --email you@twgsecurity.com --name "Your Name"
    (prompts for the password; or pipe it in with --password-stdin)
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
            tenant = Tenant(name=tenant_name)
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


def main() -> None:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("create-admin")
    ca.add_argument("--email", required=True)
    ca.add_argument("--name", default="")
    ca.add_argument("--tenant", default=DEFAULT_TENANT)
    ca.add_argument("--password-stdin", action="store_true")
    args = p.parse_args()

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

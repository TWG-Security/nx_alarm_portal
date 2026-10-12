"""Permissions beyond the admin/operator role, granted through user groups.

Admins hold every permission their company can hold; operators hold their groups' permissions.
platform.* permissions only exist in the platform company (TWG Security): a customer company's
admins don't get them, and its groups can't grant them.
Add new keys here; the Users page lists them as checkboxes on each group.
"""

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PERMISSIONS: dict[str, str] = {
    "alarms.bulk_edit": "Bulk edit alarms (admin override): mark many alarms real or false at once, "
                        "acknowledging open ones, and change verdicts already set",
    "platform.view": "See every company: switch to any company or to all companies at once (view only)",
    "platform.support": "Support other companies: acknowledge, arm/disarm, edit sites, users and settings "
                        "inside another company (logged in their audit trail as TWG support)",
    "platform.manage": "Manage companies: create companies and their first admin, branding, enable/disable sign-in",
}
PLATFORM_PERMISSIONS = {p for p in PERMISSIONS if p.startswith("platform.")}


def applicable(tenant) -> dict[str, str]:
    """The permissions a company's users can hold."""
    return {k: v for k, v in PERMISSIONS.items() if tenant.is_platform or k not in PLATFORM_PERMISSIONS}


async def load_permissions(db: AsyncSession, user, tenant) -> set[str]:
    from app.models import UserGroup, UserGroupMember
    allowed = set(applicable(tenant))
    if user.is_admin:
        return allowed
    rows = await db.scalars(select(UserGroup.permissions).join(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
                            .where(UserGroupMember.user_id == user.id, UserGroup.tenant_id == user.tenant_id))
    return {p for perms in rows.all() for p in (perms or []) if p in allowed}


def require(perm: str):
    from app.deps import current_user

    async def dep(user=Depends(current_user)):
        if not user.can(perm):
            raise HTTPException(403, f"You don't have permission for this ({PERMISSIONS[perm].split(':')[0]})")
        return user
    return dep

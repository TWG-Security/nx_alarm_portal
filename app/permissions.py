"""Permissions beyond the admin/operator role, granted through user groups.

Admins hold every permission. Operators hold the permissions of the groups they belong to.
Add new keys here; the Users page lists them as checkboxes on each group.
"""

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

PERMISSIONS: dict[str, str] = {
    "alarms.bulk_edit": "Bulk edit alarms (admin override): mark many alarms real or false at once, "
                        "acknowledging open ones, and change verdicts already set",
}


async def load_permissions(db: AsyncSession, user) -> set[str]:
    from app.models import UserGroup, UserGroupMember
    if user.is_admin:
        return set(PERMISSIONS)
    rows = await db.scalars(select(UserGroup.permissions).join(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
                            .where(UserGroupMember.user_id == user.id, UserGroup.tenant_id == user.tenant_id))
    return {p for perms in rows.all() for p in (perms or []) if p in PERMISSIONS}


def require(perm: str):
    from app.deps import current_user

    async def dep(user=Depends(current_user)):
        if not user.can(perm):
            raise HTTPException(403, f"You don't have permission for this ({PERMISSIONS[perm].split(':')[0]})")
        return user
    return dep

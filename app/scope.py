"""Which companies (tenants) a request can see, and where it may write.

Every data query filters with Scope.where(Model.tenant_id); every write to a tenant's data goes
through Scope.require_write(tenant_id) (or Scope.write_tenant() for creates).

- Ordinary users: their own company, always.
- Platform users (TWG) with platform.view choose a view, stored in the session:
    "own"  - their own company (the default)
    "all"  - every company at once, read-only overview; creates need a single company
    <id>   - one other company (support mode)
  Writing into another company also needs platform.support, and is audit-logged in that company.
- Alerting (sound, critical pop-ups) only ever follows the user's own company, but the open-alarm
  store always includes it (alerting_where) so viewing another company never hides your own alarms.
"""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import current_user
from app.models import Tenant, User


@dataclass(frozen=True)
class Scope:
    user: User
    mode: str                                # own | all | tenant
    tenant_ids: frozenset[int] | None        # None = every company
    tenant: Tenant | None                    # the single company in view (None for "all")

    @property
    def own_id(self) -> int:
        return self.user.tenant_id

    @property
    def tenant_id(self) -> int | None:
        return self.tenant.id if self.tenant else None

    @property
    def is_support(self) -> bool:
        """Viewing a company other than the user's own."""
        return self.mode != "own"

    def where(self, col):
        return true() if self.tenant_ids is None else col.in_(self.tenant_ids)

    def alerting_where(self, col):
        """Like where(), plus the user's own company: its alarms must keep reaching them."""
        return true() if self.tenant_ids is None else col.in_(self.tenant_ids | {self.own_id})

    def sees(self, tenant_id: int) -> bool:
        return self.tenant_ids is None or tenant_id in self.tenant_ids

    def can_write(self, tenant_id: int) -> bool:
        return tenant_id == self.own_id or (self.sees(tenant_id) and self.user.can("platform.support"))

    def require_write(self, tenant_id: int) -> None:
        if not self.can_write(tenant_id):
            raise HTTPException(403, "This belongs to another company. TWG support access is needed to change it.")

    def write_tenant(self) -> int:
        """The company new things (sites, users, groups, settings) are created in."""
        if self.tenant is None:
            raise HTTPException(400, "Pick a company first: this can't be done in the All companies view.")
        self.require_write(self.tenant.id)
        return self.tenant.id


async def get_scope(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> Scope:
    own = user._tenant
    sel = request.session.get("scope", "own")
    if sel == "own" or not user.can("platform.view"):
        return Scope(user, "own", frozenset({own.id}), own)
    if sel == "all":
        return Scope(user, "all", None, None)
    tenant = await db.get(Tenant, int(sel)) if str(sel).isdigit() else None
    if tenant is None or tenant.id == own.id:
        request.session["scope"] = "own"
        return Scope(user, "own", frozenset({own.id}), own)
    return Scope(user, "tenant", frozenset({tenant.id}), tenant)


async def tenant_names(db: AsyncSession) -> dict[int, str]:
    return {t.id: t.label for t in (await db.scalars(select(Tenant))).all()}

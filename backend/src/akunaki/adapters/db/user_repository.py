"""User provisioning from a verified OIDC identity.

MVP tenancy is **one user per tenant**, so a first login provisions the tenant
and its user together, in one transaction. A returning login finds the existing
user by ``(oidc_issuer, oidc_subject)`` — never by email, which is mutable and
not an identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.models import Tenant, User
from akunaki.domain.jobs import require_aware, to_utc_rfc3339
from akunaki.domain.oidc import VerifiedIdentity


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """The caller's own account and tenant.

    Deliberately excludes ``oidc_subject``: it is the identity credential the
    login flow matches on, and echoing it to a browser puts an account-linking
    key somewhere it is never needed.
    """

    user_id: str
    tenant_id: str
    email: str | None
    created_at: str
    tenant_status: str
    primary_timezone: str
    display_name: str | None


@dataclass(frozen=True, slots=True)
class ProvisionedUser:
    """A user after login provisioning.

    ``created`` is True when this login provisioned a new tenant and user.
    """

    user_id: str
    tenant_id: str
    created: bool


class UserRepository:
    """Provision and look up users from verified OIDC identities."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def upsert_from_identity(
        self,
        *,
        identity: VerifiedIdentity,
        user_id: str,
        tenant_id: str,
        now: datetime,
    ) -> ProvisionedUser:
        """Return the existing user for this identity, or provision a new one.

        Identity is ``(oidc_issuer, oidc_subject)``. The supplied ``user_id``
        and ``tenant_id`` are used only when creating; a returning user keeps
        the ids it already has so sessions and facts stay attached.
        """
        for name, value in (
            ("user_id", user_id),
            ("tenant_id", tenant_id),
        ):
            if not value:
                msg = f"{name} must be non-empty"
                raise ValueError(msg)

        now_s = to_utc_rfc3339(require_aware(now, field_name="now"))

        with self._session_factory() as session, session.begin():
            existing = session.execute(
                select(User.id, User.tenant_id).where(
                    User.oidc_issuer == identity.issuer,
                    User.oidc_subject == identity.subject,
                )
            ).one_or_none()
            if existing is not None:
                found_user_id, found_tenant_id = existing
                # Refresh the email in case the IdP changed it, but never treat
                # email as identity or as a way to merge accounts.
                session.execute(
                    select(User).where(User.id == found_user_id)
                ).scalar_one().email = identity.email
                return ProvisionedUser(
                    user_id=found_user_id,
                    tenant_id=found_tenant_id,
                    created=False,
                )

            # First login: provision the tenant and its sole user together.
            session.add(
                Tenant(
                    id=tenant_id,
                    created_at=now_s,
                    status="active",
                    primary_timezone="UTC",
                    display_name=None,
                )
            )
            session.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    oidc_issuer=identity.issuer,
                    oidc_subject=identity.subject,
                    email=identity.email,
                    created_at=now_s,
                )
            )
            return ProvisionedUser(user_id=user_id, tenant_id=tenant_id, created=True)

    def account_for(self, *, user_id: str, tenant_id: str) -> AccountSummary | None:
        """The caller's own account, or None when no such row exists.

        Scoped by **both** ids rather than the user alone: a session carries the
        tenant it was issued for, and requiring the pair means a mismatched pair
        reads nothing instead of quietly returning another tenant's row.

        None is not reachable from a validated session today — ``sessions`` has
        ``ON DELETE CASCADE`` from both ``users`` and ``tenants``, and the
        privacy scrub deletes the tenant row, so an account's sessions always
        die with it. It is still returned rather than raised because the pair is
        also what makes cross-tenant reads empty, and that case must not be an
        exception.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(
                    User.id,
                    User.email,
                    User.created_at,
                    Tenant.id,
                    Tenant.status,
                    Tenant.primary_timezone,
                    Tenant.display_name,
                )
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(User.id == user_id, User.tenant_id == tenant_id)
            ).one_or_none()
        if row is None:
            return None
        (
            found_user_id,
            email,
            created_at,
            found_tenant_id,
            tenant_status,
            primary_timezone,
            display_name,
        ) = row
        return AccountSummary(
            user_id=found_user_id,
            tenant_id=found_tenant_id,
            email=email,
            created_at=created_at,
            tenant_status=tenant_status,
            primary_timezone=primary_timezone,
            display_name=display_name,
        )

    def tenant_timezone(self, *, tenant_id: str) -> str | None:
        """The tenant's stated ``primary_timezone``, or None when no such tenant.

        For a surface with no caller to name a day — the public training
        calendar — this is the only legitimate way to decide what "today" is:
        the tenant's own stated preference, never the server's clock alone.
        None means the tenant does not exist (or was scrubbed), which the
        caller must treat as "not provisioned", not as UTC.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(Tenant.primary_timezone).where(Tenant.id == tenant_id)
            ).one_or_none()
        if row is None:
            return None
        timezone: str = row[0]
        return timezone

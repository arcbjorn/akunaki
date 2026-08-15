"""Mint, list, and revoke bearer service tokens from the terminal.

A service token is the ``Authorization: Bearer`` credential for a non-browser
caller of ``/v1/tools`` — the personal agent, an MCP adapter. There is no HTTP
route for minting on purpose: creating a credential is an operator act against
the database, not a product feature, and this way a stolen session can never
mint itself a durable token.

The raw token is printed **once** and never stored; only its hash lands in the
database.

Usage:

    uv run python scripts/service_token.py issue --name odin-personal
    uv run python scripts/service_token.py issue --name ci-probe --ttl-days 7
    uv run python scripts/service_token.py list
    uv run python scripts/service_token.py revoke --id <token-id>
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import User
from akunaki.adapters.db.service_token_repository import ServiceTokenRepository
from akunaki.config import get_settings
from akunaki.domain.tenants import SYSTEM_TENANT_ID


def _repository() -> ServiceTokenRepository:
    factory = create_session_factory(create_db_engine(get_settings()))
    return ServiceTokenRepository(factory)


def _sole_user_id() -> str:
    """Return the single human user's id, or fail with a clear message.

    Single-user product: exactly one user outside the reserved system tenant
    is the expected state. Anything else needs an explicit ``--user-id``.
    """
    factory = create_session_factory(create_db_engine(get_settings()))
    with factory() as session:
        rows = (
            session.execute(select(User.id).where(User.tenant_id != SYSTEM_TENANT_ID))
            .scalars()
            .all()
        )
    if len(rows) != 1:
        msg = f"expected exactly one user, found {len(rows)}; pass --user-id explicitly"
        raise SystemExit(msg)
    return rows[0]


def _issue(args: argparse.Namespace) -> int:
    user_id = args.user_id or _sole_user_id()
    ttl = None if args.ttl_days is None else timedelta(days=args.ttl_days)
    issued = _repository().issue(
        token_id=str(uuid.uuid4()),
        user_id=user_id,
        name=args.name,
        now=datetime.now(UTC),
        ttl=ttl,
    )
    print(f"token_id:   {issued.token_id}")
    print(f"tenant_id:  {issued.tenant_id}")
    print(f"scope:      {issued.scope.value}")
    print(f"expires_at: {issued.expires_at or 'never (revoke to retire)'}")
    print()
    print("The token below is shown once and not stored. Put it in the")
    print("caller's secret store now:")
    print()
    print(issued.token)
    return 0


def _list(args: argparse.Namespace) -> int:
    del args
    factory = create_session_factory(create_db_engine(get_settings()))
    with factory() as session:
        tenant_ids = (
            session.execute(select(User.tenant_id).where(User.tenant_id != SYSTEM_TENANT_ID))
            .scalars()
            .all()
        )
    repo = _repository()
    for tenant_id in sorted(set(tenant_ids)):
        for token_id, name, scope, revoked_at in repo.list_for_tenant(tenant_id=tenant_id):
            state = "revoked" if revoked_at else "active"
            print(f"{token_id}  {name}  {scope}  {state}")
    return 0


def _revoke(args: argparse.Namespace) -> int:
    if _repository().revoke(token_id=args.id, now=datetime.now(UTC)):
        print(f"revoked {args.id}")
        return 0
    print(f"no live token with id {args.id}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to the chosen action."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue", help="mint a new read-scoped token")
    issue.add_argument("--name", required=True, help="operator label, e.g. odin-personal")
    issue.add_argument("--user-id", default=None, help="user to bind; defaults to the sole user")
    issue.add_argument("--ttl-days", type=int, default=None, help="optional expiry in days")
    issue.set_defaults(func=_issue)

    listing = sub.add_parser("list", help="list tokens (never shows secrets)")
    listing.set_defaults(func=_list)

    revoke = sub.add_parser("revoke", help="revoke a token by id")
    revoke.add_argument("--id", required=True)
    revoke.set_defaults(func=_revoke)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())

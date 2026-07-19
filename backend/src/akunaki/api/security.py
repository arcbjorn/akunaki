"""Session cookie issuance and CSRF enforcement for cookie-authenticated routes.

Cookie policy follows the security design: ``Secure``, ``HttpOnly``,
``SameSite``, and a server-side revoke plus cookie clear on logout.

**Why CSRF is still required with SameSite.** ``SameSite=Lax`` permits
top-level cross-site *navigations* to send the cookie, so a cross-site form
POST is not universally blocked across browsers and versions. The design calls
for CSRF on cookie-authenticated state-changing requests, and this module
enforces it with a double-submit check against the per-session secret whose
hash is the only thing stored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session, sessionmaker

from akunaki.adapters.db.session_repository import SessionRepository
from akunaki.api.app import get_session_factory
from akunaki.domain.sessions import AuthenticatedSession

SESSION_COOKIE_NAME = "akunaki_session"
CSRF_HEADER_NAME = "X-Akunaki-CSRF"

# Methods that change state and therefore require a CSRF token. GET/HEAD/
# OPTIONS are exempt because they must be safe by definition.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def set_session_cookie(
    response: Response,
    *,
    token: str,
    max_age_seconds: int,
    secure: bool = True,
) -> None:
    """Attach a session cookie with the design's required attributes.

    ``secure`` is a parameter only so local HTTP development can opt out; it
    must stay true anywhere a real cookie is issued.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        # HttpOnly: script must never be able to read a session token.
        httponly=True,
        secure=secure,
        # Lax rather than Strict: an OAuth provider redirect is a top-level
        # cross-site navigation back to us, and Strict would drop the cookie.
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool = True) -> None:
    """Clear the session cookie on logout.

    Attributes must match the ones used to set it, or some browsers keep the
    original cookie alive.
    """
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _sessions(
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
) -> SessionRepository:
    return SessionRepository(session_factory)


def _unauthenticated() -> HTTPException:
    """One generic 401 for every rejection reason.

    Which check failed (unknown / expired / revoked) is server-side metrics
    only; telling a caller would help enumerate valid tokens.
    """
    return HTTPException(status_code=401, detail={"code": "unauthenticated"})


def require_session(
    request: Request,
    sessions: Annotated[SessionRepository, Depends(_sessions)],
    session_cookie: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    csrf_header: Annotated[str | None, Header(alias=CSRF_HEADER_NAME)] = None,
) -> AuthenticatedSession:
    """Authenticate a cookie session and enforce CSRF on mutations.

    Returns the validated session so routes can scope every query by its
    ``tenant_id`` rather than trusting a client-supplied one.
    """
    if not session_cookie:
        raise _unauthenticated()

    result = sessions.validate(token=session_cookie, now=datetime.now(UTC))
    if not result.ok or result.session is None:
        raise _unauthenticated()

    mutating = request.method.upper() in _STATE_CHANGING_METHODS
    if mutating and (
        not csrf_header
        or not sessions.verify_csrf(session_id=result.session.session_id, csrf_secret=csrf_header)
    ):
        # 403, not 401: the caller *is* authenticated; the request just cannot
        # be attributed to a deliberate action from our own UI.
        raise HTTPException(status_code=403, detail={"code": "forbidden"})

    return result.session


CurrentSession = Annotated[AuthenticatedSession, Depends(require_session)]

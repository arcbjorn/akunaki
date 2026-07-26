"""Drive a real Oura OAuth link from the terminal (local dev only).

The HTTP link routes need a logged-in session (tenant comes from the session)
and therefore a configured OIDC provider. This script skips that by driving the
same `OAuthLinkingService` directly, so a real Oura authorization can be walked
without standing up an identity provider first.

It is the *real* flow, not a stub: the service generates the `state` and PKCE
verifier, seals the verifier bound to its state row, and the callback leg
consumes that state exactly once — the same code the HTTP routes call.

Usage:

    # 1. print the authorize URL, persist the sealed state
    uv run python scripts/oura_link.py start

    # 2. open the URL, approve, copy the `code` from the redirect, then:
    uv run python scripts/oura_link.py finish --code <CODE> --state <STATE>

The browser lands on http://localhost:8000/... which need not be running —
the URL bar still shows `?code=...&state=...`, which is all step 2 needs.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime

from akunaki.adapters.connectors.oauth_client_factory import build_oauth_client
from akunaki.adapters.crypto.config import build_sealer
from akunaki.adapters.crypto.oauth import (
    code_challenge_s256,
    generate_code_verifier,
    generate_state,
)
from akunaki.adapters.db.connection_repository import ConnectionRepository
from akunaki.adapters.db.engine import create_db_engine, create_session_factory
from akunaki.adapters.db.models import Tenant
from akunaki.adapters.db.oauth_state_repository import OAuthStateRepository
from akunaki.api.routes.connections import DEFAULT_SCOPES
from akunaki.application.oauth_linking import OAuthLinkingService
from akunaki.config import Settings, clear_settings_cache
from akunaki.domain.jobs import to_utc_rfc3339

PROVIDER = "oura"
TENANT_ID = "dev-tenant"

# The same scopes the HTTP link route requests, so a dev link exercises the
# real consent set rather than a broader one — asking for more here would mask
# exactly the scope shortfall this script is useful for detecting (an
# under-scoped Oura token returns empty arrays, not errors).
SCOPES = DEFAULT_SCOPES[PROVIDER]


def _service(settings: Settings) -> tuple[OAuthLinkingService, object]:
    config = settings.connector_oauth(PROVIDER)
    if config is None:
        sys.exit(
            "Oura OAuth is not configured. Set AKUNAKI_OURA_CLIENT_ID / "
            "_CLIENT_SECRET / _REDIRECT_URI in backend/.env"
        )
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)

    # The linking service needs a tenant to own the connection; a real
    # deployment gets one from the session, so seed one for local use.
    now = datetime.now(UTC)
    with factory() as session, session.begin():
        if session.get(Tenant, TENANT_ID) is None:
            session.add(
                Tenant(
                    id=TENANT_ID,
                    created_at=to_utc_rfc3339(now),
                    status="active",
                    primary_timezone="UTC",
                    display_name="Local dev",
                )
            )

    service = OAuthLinkingService(
        client=build_oauth_client(PROVIDER, config),
        states=OAuthStateRepository(factory),
        connections=ConnectionRepository(factory),
        sealer=build_sealer(settings),
        generate_state=generate_state,
        generate_code_verifier=generate_code_verifier,
        code_challenge=code_challenge_s256,
        new_id=lambda: str(uuid.uuid4()),
    )
    return service, config


def cmd_start(settings: Settings) -> None:
    service, config = _service(settings)
    redirect = service.start_link(
        tenant_id=TENANT_ID,
        redirect_uri=config.redirect_uri,  # type: ignore[attr-defined]
        scopes=SCOPES,
        now=datetime.now(UTC),
    )
    print("\nOpen this URL, approve, then copy `code` and `state` from the")
    print("address bar of the page it redirects to:\n")
    print(redirect.authorize_url)
    print()


def cmd_link(settings: Settings) -> None:
    """One-shot link: open the browser, catch the redirect, exchange the code.

    Runs a throwaway HTTP listener on the redirect URI's port so the browser's
    callback is captured directly — no copying `code`/`state` by hand.
    """
    import http.server
    import threading
    import webbrowser
    from urllib.parse import parse_qs, urlparse

    service, config = _service(settings)
    redirect_uri: str = config.redirect_uri  # type: ignore[attr-defined]
    parsed = urlparse(redirect_uri)
    port = parsed.port or 80

    started = service.start_link(
        tenant_id=TENANT_ID,
        redirect_uri=redirect_uri,
        scopes=SCOPES,
        now=datetime.now(UTC),
    )

    caught: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            for key in ("code", "state", "error"):
                value = query.get(key, [""])[0]
                if value:
                    caught[key] = value
            ok = "code" in caught and "state" in caught
            body = (
                b"<h2>Linked. You can close this tab.</h2>"
                if ok
                else b"<h2>Authorization failed. Check the terminal.</h2>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, format: str, *args: object) -> None:
            """Silence the default stderr access log."""

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"\nListening on {parsed.scheme}://{parsed.hostname}:{port} for the callback.")
    print("Opening your browser to approve the Oura authorization...\n")
    print(started.authorize_url, "\n")
    webbrowser.open(started.authorize_url)

    if not done.wait(timeout=300):
        server.shutdown()
        sys.exit("timed out waiting for the callback (5 min)")
    server.shutdown()

    if "error" in caught:
        sys.exit(f"provider returned an error: {caught['error']}")

    cmd_finish(settings, code=caught["code"], state=caught["state"], service=service)


def cmd_finish(
    settings: Settings,
    *,
    code: str,
    state: str,
    service: OAuthLinkingService | None = None,
) -> None:
    config = settings.connector_oauth(PROVIDER)
    if service is None:
        service, config = _service(settings)  # type: ignore[assignment]
    result = service.complete_link(
        state=state,
        code=code,
        redirect_uri=config.redirect_uri,  # type: ignore[union-attr]
        now=datetime.now(UTC),
    )
    if not result.ok or result.connection is None:
        sys.exit(f"link failed: {result.rejection}")
    conn = result.connection
    print(f"\nlinked: connection_id={conn.connection_id} status={conn.status.value}")
    print("tokens are sealed at rest; run the sync next.\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("link")
    sub.add_parser("start")
    finish = sub.add_parser("finish")
    finish.add_argument("--code", required=True)
    finish.add_argument("--state", required=True)
    args = parser.parse_args()

    clear_settings_cache()
    settings = Settings()

    if args.cmd == "link":
        cmd_link(settings)
    elif args.cmd == "start":
        cmd_start(settings)
    else:
        cmd_finish(settings, code=args.code, state=args.state)


if __name__ == "__main__":
    main()

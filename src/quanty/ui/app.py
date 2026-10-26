"""The dashboard: a React app (ui/web, built into ui/dist) over a JSON API and a live stream.
Run it with `quanty ui`. Reads databases read-only; see ui/api.py."""

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from quanty.ui.api import Api, router
from quanty.ui.auth import Auth
from quanty.venues.auth import keys

DIST = Path(__file__).parent / "dist"
NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>quanty</title>
<p style="font: 15px system-ui; margin: 40px">The dashboard's front end isn't built.
Run <code>npm --prefix web ci && npm --prefix web run build</code> from the repository root.</p>"""


_FROM_ENV = "from .env"
PUBLIC = ("/login", "/assets/", "/favicon")


def create_app(
    data: Path, refresh_s: int = 2, password_hash: str | None = _FROM_ENV, demo: bool = False
) -> FastAPI:
    """refresh_s is kept for callers; the page is pushed changes over a WebSocket instead.
    password_hash: the login hash (QUANTY_UI_PASSWORD by default); None = no login. demo: serving
    `quanty demo` (synthetic markets in a temporary folder: no project to check)."""
    if password_hash == _FROM_ENV:
        secret = keys().quanty_ui_password
        password_hash = secret.get_secret_value().strip() if secret else None
    auth = Auth(password_hash) if password_hash else None
    app = FastAPI(title="quanty", docs_url=None, redoc_url=None)
    api = Api(data, auth, demo=demo)

    if auth is not None:

        @app.middleware("http")
        async def require_login(
            request: Request, call_next: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            if request.url.path.startswith(PUBLIC) or auth.valid(request):
                return await call_next(request)
            return auth.deny(request)

        @app.get("/login", include_in_schema=False)
        def login_page(request: Request) -> Response:
            return RedirectResponse("/", status_code=303) if auth.valid(request) else auth.page(request)

        @app.post("/login", include_in_schema=False)
        async def login(request: Request) -> Response:
            return await auth.login(request)

        @app.post("/logout", include_in_schema=False)
        def logout(request: Request) -> Response:
            return auth.logout(request)

    app.include_router(router(api))
    if (DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    def spa(path: str) -> HTMLResponse | FileResponse:
        if path == "api" or path.startswith("api/"):  # not a page: a route this server doesn't have
            raise HTTPException(
                404, f"no /{path} here: the dashboard server may be older than the page (restart it)"
            )
        if path and (DIST / path).is_file() and not path.endswith(".html"):
            return FileResponse(DIST / path)
        index = DIST / "index.html"
        if not index.exists():
            return HTMLResponse(NOT_BUILT)
        # the control token rides in the page; another site can't read it
        page = index.read_text().replace("__QUANTY_TOKEN__", api.token)
        if auth is not None:  # the app shows "Sign out"
            page = page.replace(
                '<meta name="quanty-token"',
                '<meta name="quanty-auth" content="1" />\n    <meta name="quanty-token"',
            )
        return HTMLResponse(page)

    return app

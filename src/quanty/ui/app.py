"""The dashboard: a React app (ui/web, built into ui/dist) over a JSON API and a live stream.
Run it with `quanty ui`. Reads databases read-only; see ui/api.py."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from quanty.ui.api import Api, router

DIST = Path(__file__).parent / "dist"
NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>quanty</title>
<p style="font: 15px system-ui; margin: 40px">The dashboard's front end isn't built.
Run <code>npm --prefix web ci && npm --prefix web run build</code> from the repository root.</p>"""


def create_app(data: Path, refresh_s: int = 2) -> FastAPI:
    """refresh_s is kept for callers; the page is pushed changes over a WebSocket instead."""
    app = FastAPI(title="quanty", docs_url=None, redoc_url=None)
    api = Api(data)
    app.include_router(router(api))
    if (DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    def spa(path: str) -> HTMLResponse | FileResponse:
        if path and (DIST / path).is_file() and not path.endswith(".html"):
            return FileResponse(DIST / path)
        index = DIST / "index.html"
        if not index.exists():
            return HTMLResponse(NOT_BUILT)
        # the control token rides in the page; another site can't read it
        return HTMLResponse(index.read_text().replace("__QUANTY_TOKEN__", api.token))

    return app

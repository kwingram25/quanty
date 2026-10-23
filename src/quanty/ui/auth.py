"""Optional dashboard login: one password, set as a salted scrypt hash (QUANTY_UI_PASSWORD in .env,
made with `quanty password`). Off when unset, so local use stays one command.

When on, every page, API call and live stream needs a session cookie from /login. Sessions live
in memory (a restart signs everyone out) for 7 days; the cookie is HttpOnly and SameSite=Strict, and
Secure when the request came over HTTPS (directly or via a proxy's X-Forwarded-Proto). Five wrong
passwords from one address lock it out for a minute. Without HTTPS the password crosses the network
in the clear: put the dashboard behind a TLS proxy (see docs/deploying.md) before exposing it.
"""

import base64
import hashlib
import hmac
import secrets
import time
from html import escape
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.requests import HTTPConnection

COOKIE = "quanty_session"
SESSION_S = 7 * 24 * 3600
MAX_FAILS, LOCKOUT_S = 5, 60
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(base64.b64decode(digest)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


class Auth:
    def __init__(self, password_hash: str) -> None:
        if not password_hash.startswith("scrypt$"):
            raise ValueError(
                "QUANTY_UI_PASSWORD must be a hash from `quanty password`, not the password itself"
            )
        self.password_hash = password_hash
        self._sessions: dict[str, float] = {}  # token -> expiry (unix s)
        self._fails: dict[str, tuple[int, float]] = {}  # address -> (failures, time of the last one)

    # --- sessions ---

    def valid(self, request: HTTPConnection) -> bool:
        token = request.cookies.get(COOKIE, "")
        expiry = self._sessions.get(token)
        if expiry is None:
            return False
        if expiry < time.time():
            del self._sessions[token]
            return False
        return True

    def _locked(self, who: str) -> float:
        n, last = self._fails.get(who, (0, 0.0))
        wait = last + LOCKOUT_S - time.time()
        return wait if n >= MAX_FAILS and wait > 0 else 0.0

    # --- routes ---

    def page(self, request: Request, error: str = "", nxt: str | None = None) -> HTMLResponse:
        """The sign-in form; nxt is where to go after (kept through failed attempts)."""
        nxt = nxt or request.query_params.get("next", "/")
        return HTMLResponse(
            LOGIN_PAGE.format(next=escape(nxt), error=f'<p class="err">{escape(error)}</p>' if error else "")
        )

    async def login(self, request: Request) -> Response:
        who = request.client.host if request.client else "?"
        form = await request.form()
        nxt = str(form.get("next") or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"  # only back into this site
        wait = self._locked(who)
        if wait:
            return self.page(request, f"Too many attempts. Try again in {wait:.0f} s.", nxt)
        if not verify_password(str(form.get("password") or ""), self.password_hash):
            n, last = self._fails.get(who, (0, 0.0))
            fresh = time.time() - last > LOCKOUT_S  # older failures are forgotten
            self._fails[who] = (1 if fresh else n + 1, time.time())
            return self.page(request, "Wrong password.", nxt)
        self._fails.pop(who, None)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + SESSION_S
        resp = RedirectResponse(nxt, status_code=303)
        https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        resp.set_cookie(
            COOKIE, token, max_age=SESSION_S, httponly=True, samesite="strict", secure=https, path="/"
        )
        return resp

    def logout(self, request: Request) -> Response:
        self._sessions.pop(request.cookies.get(COOKIE, ""), None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    def deny(self, request: Request) -> Response:
        """What an unsigned-in request gets: a sign-in redirect for pages, 401 for the API."""
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "sign in first"}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>quanty · sign in</title>
<style>
  :root {{ color-scheme: light; --page: #f3f5f8; --surface: #fff; --ink: #141a22; --muted: #6b7482;
          --border: #dde2e9; --accent: #2a78d6; --bad: #c2312f; }}
  @media (prefers-color-scheme: dark) {{ :root {{ color-scheme: dark; --page: #0c0f14; --surface: #141920;
          --ink: #e9edf2; --muted: #7d8796; --border: #252d38; --accent: #3987e5; --bad: #f06a68; }} }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--page);
         color: var(--ink); font: 14px/1.45 "IBM Plex Sans", system-ui, sans-serif; }}
  form {{ width: min(340px, calc(100vw - 32px)); padding: 24px; border-radius: 14px;
         background: var(--surface);
         border: 1px solid var(--border); box-shadow: 0 4px 16px rgba(0,0,0,.06); }}
  h1 {{ margin: 0 0 4px; font-size: 18px; }} p {{ margin: 0 0 16px; color: var(--muted); }}
  input {{ width: 100%; height: 38px; padding: 0 10px; border-radius: 8px; border: 1px solid var(--border);
          background: var(--page); color: var(--ink); font: inherit; }}
  input:focus-visible, button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  button {{ width: 100%; height: 38px; margin-top: 12px; border: none; border-radius: 8px;
           background: var(--accent);
           color: #fff; font: inherit; font-weight: 600; cursor: pointer; }}
  .err {{ color: var(--bad); margin: 10px 0 0; }}
</style></head>
<body><form method="post" action="/login">
  <h1>quanty</h1><p>Sign in to the dashboard.</p>
  <input type="hidden" name="next" value="{next}">
  <input type="password" name="password" autocomplete="current-password" aria-label="Password"
         placeholder="Password" autofocus required>
  <button type="submit">Sign in</button>{error}
</form></body></html>"""

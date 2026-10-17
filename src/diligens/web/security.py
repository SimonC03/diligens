"""Keep the local app local: same-origin writes, no framing, no oversized uploads.

The app binds to 127.0.0.1, and TrustedHostMiddleware refuses other Host headers (DNS
rebinding). This middleware adds what a browser needs to keep other sites out: a state
changing request must come from the app's own pages, and every response forbids framing
(clickjacking) and loads scripts and styles only from the app itself.

Browsers mark every request with Sec-Fetch-Site, which a web page cannot set; that is the
primary check. The Origin header is only a fallback for browsers without it, because a
browser may report the origin of the app's own form posts as "null".
"""

from html import escape

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
OWN_PAGES = ("same-origin", "none")  # "none": typed by the user, not sent by a page
MAX_UPLOAD = 50 * 1024 * 1024
FORM_OVERHEAD = 1024 * 1024  # multipart boundaries and the other form fields

HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # Addresses inside the app (which name analyses) never reach another site.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",  # client figures should not linger in the browser cache
}

CROSS_SITE = "Förfrågan kom från en annan webbplats och avvisades."
TOO_LARGE = "Filen är större än 50 MB. Exportera ett räkenskapsår åt gången."


def refusal(message: str, status_code: int) -> Response:
    """A short Swedish page; the app's templates are not reachable from middleware."""
    body = (
        '<!doctype html><html lang="sv"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="/static/app.css"><title>Diligens</title></head>'
        '<body><main class="main"><div class="panel" style="max-width: 560px">'
        f'<div class="empty"><h2>Förfrågan avvisades</h2><p>{escape(message)}</p>'
        '<a class="btn" href="/">Till översikten</a></div></div></main></body></html>'
    )
    return HTMLResponse(body, status_code=status_code, headers=HEADERS)


def from_own_pages(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in OWN_PAGES
    origin = request.headers.get("origin")
    if origin is None:
        return True  # not a browser page: a script on this computer, or the tests
    return origin == f"{request.url.scheme}://{request.headers.get('host', '')}"


class LocalOnly(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in SAFE_METHODS:
            if not from_own_pages(request):
                return refusal(CROSS_SITE, 403)
            length = request.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_UPLOAD + FORM_OVERHEAD:
                # Refused before the body is read, so the file never reaches a temp folder.
                return refusal(TOO_LARGE, 413)
        response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers.setdefault(name, value)
        return response

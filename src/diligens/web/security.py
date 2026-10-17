"""Keep the local app local: same-origin writes, no framing, no oversized uploads.

The app binds to 127.0.0.1, and TrustedHostMiddleware refuses other Host headers (DNS
rebinding). This middleware adds what a browser needs to keep other sites out: a state
changing request must come from the app's own origin, port included, and every response
forbids framing (clickjacking) and loads scripts and styles only from the app itself.
"""

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
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
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",  # client figures should not linger in the browser cache
}

CROSS_SITE = "Förfrågan kom från en annan webbplats och avvisades."
TOO_LARGE = "Filen är större än 50 MB. Exportera ett räkenskapsår åt gången."


class LocalOnly(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            own = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin is not None and origin != own:
                return Response(CROSS_SITE, status_code=403, headers=HEADERS)
            if origin is None and request.headers.get("sec-fetch-site") == "cross-site":
                return Response(CROSS_SITE, status_code=403, headers=HEADERS)
            length = request.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_UPLOAD + FORM_OVERHEAD:
                # Refused before the body is read, so the file never reaches a temp folder.
                return Response(TOO_LARGE, status_code=413, headers=HEADERS)
        response = await call_next(request)
        for name, value in HEADERS.items():
            response.headers.setdefault(name, value)
        return response

"""Diligens as a local web app: due diligence and screening in one interface.

Runs on 127.0.0.1 only. Pages are rendered on the server from the same results the CLI and
the MCP server use; the browser never computes a figure. Uploaded files and results stay in
the local workspace (Settings.diligens_workspace). Routes live in web/routes/, one module
per area; what they share is the Ctx in web/context.py.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from diligens.config import Settings, get_settings
from diligens.sources.bolagsverket import BolagsverketClient, make_bolagsverket_client
from diligens.sources.scb import ScbClient, make_scb_client
from diligens.web.context import HERE, Ctx, make_templates
from diligens.web.jobs import JobRunner
from diligens.web.routes import analyses, general, screening
from diligens.web.security import LocalOnly
from diligens.web.workspace import Workspace

LOCAL_HOSTS = ("127.0.0.1", "localhost")
DEMO_FILE = Path("data/demo/demo_bolag.se")
INVALID = "Uppgifterna kunde inte läsas. Kontrollera formuläret och försök igen."


def create_app(
    settings_provider: Callable[[], Settings] = get_settings,
    workspace: Path | None = None,
    scb_factory: Callable[[Settings], ScbClient] = make_scb_client,
    bolagsverket_factory: Callable[[Settings], BolagsverketClient] = make_bolagsverket_client,
    inline_jobs: bool = False,
    demo_file: Path | None = DEMO_FILE,
    allowed_hosts: Sequence[str] = LOCAL_HOSTS,
) -> FastAPI:
    """The app. Factories, the workspace and hosts are injectable so tests run offline."""
    ctx = Ctx(
        settings_provider=settings_provider,
        space=Workspace(workspace or settings_provider().diligens_workspace),
        jobs=JobRunner(inline=inline_jobs),
        scb_factory=scb_factory,
        bolagsverket_factory=bolagsverket_factory,
        demo_file=demo_file,
        templates=make_templates(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        ctx.jobs.shutdown()  # Ctrl+C does not wait for a running screening

    app = FastAPI(
        title="Diligens", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.ctx = ctx
    app.add_middleware(LocalOnly)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    for module in (general, analyses, screening):
        app.include_router(module.router)

    def error_page(request: Request, code: int, message: str) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": message}, status_code=code)
        return ctx.page(request, "error.html", "", status_code=code, code=code, message=message)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        # Starlette's own 404 for an unknown address says "Not Found"; ours are Swedish.
        message = "Sidan finns inte." if exc.detail == "Not Found" else str(exc.detail)
        return error_page(request, exc.status_code, message)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError) -> Response:
        return error_page(request, 400, INVALID)

    return app

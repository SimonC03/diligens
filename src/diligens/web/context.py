"""What every route needs: settings, workspace, jobs, the sources and page rendering."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from diligens import __version__
from diligens.config import Settings
from diligens.models import AnalysisResult, ScreeningResult
from diligens.sources.bolagsverket import BolagsverketClient
from diligens.sources.scb import ScbClient
from diligens.web import views
from diligens.web.jobs import JobRunner
from diligens.web.workspace import AnalysisMeta, ScreeningMeta, Workspace

HERE = Path(__file__).parent


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(
        tsek=views.tsek,
        pct=views.percent,
        month=views.month_label,
        org=views.org_display,
        sek=views.sek,
        conf=views.confidence,
        integer=views.integer,
    )
    env.globals["version"] = __version__
    return templates


@dataclass
class Ctx:
    settings_provider: Callable[[], Settings]
    space: Workspace
    jobs: JobRunner
    scb_factory: Callable[[Settings], ScbClient]
    bolagsverket_factory: Callable[[Settings], BolagsverketClient]
    demo_file: Path | None
    templates: Jinja2Templates

    @property
    def settings(self) -> Settings:
        return self.settings_provider()

    @property
    def demo_available(self) -> bool:
        return self.demo_file is not None and self.demo_file.is_file()

    def status(self) -> dict[str, Any]:
        """Which connections are configured; booleans and the model name, never a key."""
        s = self.settings
        return {
            "ai": s.llm_enabled,
            "model": s.diligens_model,
            "scb": s.scb_api_key is not None,
            "bolagsverket": s.bolagsverket_client_id is not None
            and s.bolagsverket_client_secret is not None,
        }

    def page(
        self, request: Request, name: str, nav: str, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        return self.templates.TemplateResponse(
            request,
            name,
            {"nav": nav, "status": self.status(), **context},
            status_code=status_code,
        )

    def analysis(self, item_id: str) -> tuple[AnalysisMeta, AnalysisResult]:
        try:
            return self.space.analysis(item_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, "Analysen finns inte.") from exc

    def analysis_meta(self, item_id: str) -> AnalysisMeta:
        try:
            return self.space.analysis_meta(item_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, "Analysen finns inte.") from exc

    def screening(self, item_id: str) -> tuple[ScreeningMeta, ScreeningResult]:
        try:
            return self.space.screening(item_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, "Screeningen finns inte.") from exc


def get_ctx(request: Request) -> Ctx:
    ctx: Ctx = request.app.state.ctx
    return ctx


CtxDep = Annotated[Ctx, Depends(get_ctx)]

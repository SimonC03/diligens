"""Diligens as a local web app: due diligence and screening in one interface.

Runs on 127.0.0.1 only. Pages are rendered on the server from the same results the CLI and
the MCP server use; the browser never computes a figure. Uploaded files and results stay in
the local workspace (Settings.diligens_workspace).
"""

import contextlib
import datetime as dt
import logging
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from diligens import __version__
from diligens.agent.client import make_client
from diligens.agent.memo import write_memo
from diligens.analysis.bridge import with_selection
from diligens.config import Settings, get_settings
from diligens.export.excel import CATEGORY_LABELS, export_dd_pack
from diligens.export.longlist import export_long_list
from diligens.models import AnalysisResult, ScreeningCriteria
from diligens.pipeline import ANALYSIS_STAGES, run_analysis
from diligens.screening import (
    SCREENING_STAGES,
    TOP_DEFAULT,
    UNANSWERED,
    retry_unavailable,
    run_screening,
)
from diligens.sie.parser import parse_sie
from diligens.sources.bolagsverket import (
    BolagsverketClient,
    BolagsverketError,
    make_bolagsverket_client,
)
from diligens.sources.scb import ScbClient, ScbError, make_scb_client
from diligens.web import views
from diligens.web.jobs import JobRunner, Reporter
from diligens.web.workspace import AnalysisMeta, ScreeningMeta, Workspace

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
MAX_UPLOAD = 50 * 1024 * 1024
SAVE_STAGE = "Sparar och skapar Excel"
SCREEN_SAVE_STAGE = "Sparar long list"
RETRY_STAGE = "Hämtar återstående årsredovisningar"
DEMO_FILE = Path("data/demo/demo_bolag.se")
FORM_TABLES = ("naringsgrenkoder", "omsklkoder", "anstklkoder", "lankoder")

# Tabs of an analysis, grouped as the work flows: overview, figures, earnings quality, basis.
TABS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Översikt", [("sammanfattning", "Sammanfattning")]),
    (
        "Finansiell information",
        [
            ("resultatrakning", "Resultaträkning"),
            ("balansrakning", "Balansräkning"),
            ("rorelsekapital", "Rörelsekapital"),
            ("nettoskuld", "Nettoskuld"),
        ],
    ),
    (
        "Resultatets kvalitet",
        [("brygga", "EBITDA-brygga"), ("flaggor", "Flaggade poster"), ("fragor", "DD-frågor")],
    ),
    ("Underlag", [("avstamning", "Avstämning"), ("memo", "Memo")]),
]
TAB_NAMES = {slug: label for _, tabs in TABS for slug, label in tabs}


class Selection(BaseModel):
    inkludera: list[bool]


class OriginCheck(BaseHTTPMiddleware):
    """Refuse state changing requests sent by another site to the local app."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).hostname not in ALLOWED_HOSTS:
                return Response("Förfrågan kom från en annan webbplats och avvisades.", 403)
        return await call_next(request)


def _criteria_title(criteria: ScreeningCriteria, labels: dict[str, dict[str, str]]) -> str:
    sni = ", ".join(criteria.sni)
    parts = [f"SNI {sni}"]
    if criteria.turnover_classes:
        parts.append("oms.klass " + ", ".join(criteria.turnover_classes))
    if criteria.employee_classes:
        parts.append("anst.klass " + ", ".join(criteria.employee_classes))
    if criteria.counties:
        names = labels.get("lankoder", {})
        parts.append(", ".join(names.get(c, c) for c in criteria.counties))
    return " · ".join(parts)


def create_app(
    settings_provider: Callable[[], Settings] = get_settings,
    workspace: Path | None = None,
    scb_factory: Callable[[Settings], ScbClient] = make_scb_client,
    bolagsverket_factory: Callable[[Settings], BolagsverketClient] = make_bolagsverket_client,
    inline_jobs: bool = False,
    demo_file: Path | None = DEMO_FILE,
) -> FastAPI:
    """The app. Factories and the workspace are injectable so tests run without network."""
    space = Workspace(workspace or settings_provider().diligens_workspace)
    jobs = JobRunner(inline=inline_jobs)
    app = FastAPI(title="Diligens", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(OriginCheck)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters["tsek"] = views.tsek
    env.filters["pct"] = views.percent
    env.filters["month"] = views.month_label
    env.filters["org"] = views.org_display
    env.filters["sek"] = views.sek
    env.filters["conf"] = views.confidence
    env.filters["integer"] = views.integer
    env.globals["version"] = __version__

    def status() -> dict[str, Any]:
        s = settings_provider()
        return {
            "ai": s.llm_enabled,
            "model": s.diligens_model,
            "scb": s.scb_api_key is not None,
            "bolagsverket": s.bolagsverket_client_id is not None
            and s.bolagsverket_client_secret is not None,
        }

    def page(request: Request, name: str, nav: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {"nav": nav, "status": status(), **context}
        )

    def analysis_or_404(item_id: str) -> tuple[AnalysisMeta, AnalysisResult]:
        try:
            return space.analysis(item_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, "Analysen finns inte.") from exc

    # ---------------------------------------------------------------- overview

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        analyses = space.analyses()
        screenings = space.screenings()
        recent: list[dict[str, Any]] = [
            {
                "kind": "Due diligence",
                "title": a.company_name,
                "detail": f"{a.org_nr} · {a.basis} {a.period}",
                "created": a.created,
                "url": f"/analyser/{a.id}",
            }
            for a in analyses[:6]
        ] + [
            {
                "kind": "Screening",
                "title": s.title,
                "detail": f"{s.candidates} kandidater · {s.reports} årsredovisningar",
                "created": s.created,
                "url": f"/screening/{s.id}",
            }
            for s in screenings[:6]
        ]
        recent.sort(key=lambda r: r["created"], reverse=True)
        return page(
            request,
            "overview.html",
            "oversikt",
            recent=recent[:8],
            counts={"analyses": len(analyses), "screenings": len(screenings)},
        )

    @app.get("/metod", response_class=HTMLResponse)
    def method(request: Request) -> HTMLResponse:
        categories = [
            (
                CATEGORY_LABELS["engangspost"],
                "Engångsposter, till exempel flytt, tvist eller "
                "rekrytering. Läggs tillbaka i justerad EBITDA.",
            ),
            (
                CATEGORY_LABELS["agarrelaterad"],
                "Kostnader i ägarens sfär, till exempel privat "
                "bil eller arvode till ägarbolaget. Läggs tillbaka i sin helhet.",
            ),
            (
                CATEGORY_LABELS["periodisering"],
                "Intäkt eller kostnad bokförd i fel period. Flyttas ut ur perioden.",
            ),
            (
                CATEGORY_LABELS["narstaende"],
                "Transaktion med närstående part. Listas med "
                "Inkludera av, rådgivaren bedömer villkoren.",
            ),
            (
                CATEGORY_LABELS["avvikelse"],
                "Ovanligt mönster som är värt en fråga men inte en justering.",
            ),
        ]
        return page(request, "method.html", "metod", categories=categories)

    # --------------------------------------------------------------- analyses

    @app.get("/analyser", response_class=HTMLResponse)
    def analyses(request: Request) -> HTMLResponse:
        return page(request, "analyses.html", "analyser", analyses=space.analyses())

    @app.get("/analyser/ny", response_class=HTMLResponse)
    def new_analysis(request: Request) -> HTMLResponse:
        demo = demo_file is not None and demo_file.is_file()
        return page(request, "analysis_new.html", "analyser", demo=demo)

    @app.post("/analyser")
    async def start_analysis(
        request: Request,
        fil: Annotated[UploadFile | None, File()] = None,
        med_ai: Annotated[str | None, Form()] = None,
        narstaende: Annotated[str, Form()] = "",
        demo: Annotated[str | None, Form()] = None,
    ) -> Response:
        item_id = space.new_id()
        target = space.upload_path(item_id)
        if demo and demo_file is not None and demo_file.is_file():
            target.write_bytes(demo_file.read_bytes())
            source_name = demo_file.name
        elif fil is not None and fil.filename:
            size = 0
            with target.open("wb") as out:
                while chunk := await fil.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        out.close()
                        space.delete_analysis(item_id)
                        return page(
                            request,
                            "analysis_new.html",
                            "analyser",
                            demo=demo_file is not None and demo_file.is_file(),
                            error="Filen är större än 50 MB. Exportera ett räkenskapsår åt gången.",
                        )
                    out.write(chunk)
            source_name = Path(fil.filename).name
        else:
            space.delete_analysis(item_id)
            return page(
                request,
                "analysis_new.html",
                "analyser",
                demo=demo_file is not None and demo_file.is_file(),
                error="Välj en SIE4-fil att analysera.",
            )

        use_llm = med_ai is not None
        related = [line.strip() for line in narstaende.splitlines() if line.strip()]
        settings = settings_provider()

        def work(report: Reporter) -> str:
            try:
                result = run_analysis(
                    target,
                    use_llm=use_llm,
                    settings=settings,
                    related_parties=related,
                    progress=report.stage,
                )
                report.stage(SAVE_STAGE)
                bridge = result.bridge
                meta = AnalysisMeta(
                    id=item_id,
                    created=dt.datetime.now(),
                    source_name=source_name,
                    company_name=result.company_name,
                    org_nr=result.org_nr,
                    period=f"{bridge.period[0]} till {bridge.period[1]}",
                    basis=bridge.basis,
                    use_llm=result.llm_used,
                    model=result.model,
                    related_parties=related,
                    reconciled=result.reconciliation.ok,
                )
                space.save_analysis(meta, result)
                export_dd_pack(result, space.analysis_dir(item_id))
            except BaseException:
                space.delete_analysis(item_id)
                raise
            return f"/analyser/{item_id}"

        job = jobs.start(
            "analys",
            f"Analyserar {source_name}",
            [*ANALYSIS_STAGES, SAVE_STAGE],
            work,
            back="/analyser/ny",
        )
        return RedirectResponse(f"/jobb/{job.id}", status_code=303)

    @app.get("/analyser/{item_id}")
    def analysis_home(item_id: str) -> Response:
        analysis_or_404(item_id)
        return RedirectResponse(f"/analyser/{item_id}/sammanfattning", status_code=307)

    @app.get("/analyser/{item_id}/verifikat/{ref}", response_class=HTMLResponse)
    def voucher(request: Request, item_id: str, ref: str) -> HTMLResponse:
        """One voucher as booked, read again from the uploaded SIE file."""
        meta, result = analysis_or_404(item_id)
        ledger = parse_sie(space.upload_path(item_id))
        view = views.voucher_view(ledger, ref)
        if view is None:
            raise HTTPException(404, "Verifikatet finns inte i SIE-filen.")
        flagged = next((r for r in views.flag_rows(result) if r.flag.voucher_ref == ref), None)
        fragment = request.query_params.get("del") == "1"
        return page(
            request,
            "_voucher.html" if fragment else "voucher.html",
            "analyser",
            meta=meta,
            voucher=view,
            flagged=flagged,
        )

    @app.get("/analyser/{item_id}/{tab}", response_class=HTMLResponse)
    def analysis_tab(request: Request, item_id: str, tab: str) -> HTMLResponse:
        if tab not in TAB_NAMES:
            raise HTTPException(404, "Fliken finns inte.")
        meta, result = analysis_or_404(item_id)
        context: dict[str, Any] = {
            "meta": meta,
            "r": result,
            "tab": tab,
            "tabs": TABS,
            "tab_name": TAB_NAMES[tab],
            "months": result.statements.months,
        }
        if tab == "sammanfattning":
            kpis, bridge = result.kpis, result.bridge
            series = kpis.ltm if bridge.basis == "LTM" else kpis.ytd
            month = bridge.month
            rows = views.flag_rows(result)
            context.update(
                series=series,
                month=month,
                chart=views.monthly_chart(result),
                review=sum(1 for r in rows if r.status == "Kräver granskning"),
                adjusted_margin=(
                    bridge.adjusted / bridge.adjusted_revenue if bridge.adjusted_revenue else None
                ),
                nwc_last=kpis.nwc.values.get(kpis.months[-1]),
                adjustments=bridge.adjusted - bridge.reported,
                net_cash=kpis.net_debt < 0,
                estimated_tax=any(i.kind == "beraknad_skatt" for i in kpis.net_debt_items),
            )
        elif tab == "resultatrakning":
            context["rows"] = views.income_rows(result)
        elif tab == "balansrakning":
            context["rows"] = views.balance_rows(result)
        elif tab == "rorelsekapital":
            context["rows"] = views.nwc_rows(result)
        elif tab == "brygga":
            bridge = result.bridge
            context.update(
                items=[*bridge.items, *bridge.candidates],
                categories=CATEGORY_LABELS,
                adjusted_margin=(
                    bridge.adjusted / bridge.adjusted_revenue if bridge.adjusted_revenue else None
                ),
            )
        elif tab == "flaggor":
            rows = views.flag_rows(result)
            chosen = request.query_params.get("status", "")
            context.update(
                rows=[r for r in rows if not chosen or r.status == chosen],
                counts=views.status_counts(rows),
                chosen=chosen,
                total=len(rows),
            )
        elif tab == "avstamning":
            rec = result.reconciliation
            context.update(
                off=[row for row in rec.rows if row.difference != 0],
                years={
                    fy.index: f"{fy.start} till {fy.end}" for fy in result.statements.fiscal_years
                },
            )
        elif tab == "memo":
            context["memo_html"] = views.memo_html(result.memo)
        return page(request, f"tab_{tab}.html", "analyser", **context)

    @app.post("/api/analyser/{item_id}/urval")
    def select(item_id: str, body: Selection) -> JSONResponse:
        """Save the advisor's Inkludera choices; the totals come from with_selection."""
        meta, _ = analysis_or_404(item_id)
        original = space.proposed(item_id).bridge
        proposed = [i.included for i in [*original.items, *original.candidates]]
        try:
            bridge = with_selection(original, body.inkludera)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        new_selection = None if body.inkludera == proposed else body.inkludera
        if new_selection != meta.selection:
            meta = meta.model_copy(update={"selection": new_selection})
            space.save_meta(meta)
        margin = bridge.adjusted / bridge.adjusted_revenue if bridge.adjusted_revenue else None
        return JSONResponse(
            {
                "justerad_ebitda": views.tsek(bridge.adjusted),
                "justerad_omsattning": views.tsek(bridge.adjusted_revenue),
                "marginal": views.percent(margin),
                "memo_aktuellt": meta.memo_current,
                "andrat": new_selection is not None,
            }
        )

    @app.post("/analyser/{item_id}/memo")
    def rewrite_memo(item_id: str) -> Response:
        meta, result = analysis_or_404(item_id)
        settings = settings_provider()

        def work(report: Reporter) -> str:
            report.stage("Skriver memo")
            client = make_client(settings) if meta.use_llm and settings.llm_enabled else None
            memo = write_memo(result, client, model=meta.model or settings.diligens_model)
            space.save_memo(item_id, memo)
            # The memo matches the selection it was written with, even if it changed meanwhile.
            current = space.analysis_meta(item_id)
            space.save_meta(current.model_copy(update={"memo_selection": meta.selection}))
            return f"/analyser/{item_id}/memo"

        job = jobs.start(
            "memo",
            f"Memo för {meta.company_name}",
            ["Skriver memo"],
            work,
            back=f"/analyser/{item_id}/memo",
        )
        return RedirectResponse(f"/jobb/{job.id}", status_code=303)

    @app.get("/analyser/{item_id}/fil/dd_pack.xlsx")
    def download_pack(item_id: str) -> FileResponse:
        meta, result = analysis_or_404(item_id)
        path = export_dd_pack(result, space.analysis_dir(item_id))
        return FileResponse(
            path,
            filename=f"{meta.company_name} DD-underlag.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.get("/analyser/{item_id}/fil/memo.md")
    def download_memo(item_id: str) -> FileResponse:
        meta, _ = analysis_or_404(item_id)
        return FileResponse(
            space.analysis_dir(item_id) / "memo.md",
            filename=f"{meta.company_name} memo.md",
            media_type="text/markdown; charset=utf-8",
        )

    @app.post("/analyser/{item_id}/ta-bort")
    def delete_analysis(item_id: str) -> Response:
        analysis_or_404(item_id)
        space.delete_analysis(item_id)
        return RedirectResponse("/analyser", status_code=303)

    # --------------------------------------------------------------- screening

    def code_tables(refresh: bool = False) -> tuple[dict[str, dict[str, str]], str]:
        """Code tables for the form, from the workspace cache or SCB; error text if neither."""
        tables: dict[str, dict[str, str]] = {}
        missing = [n for n in FORM_TABLES if refresh or space.code_table(n) is None]
        if missing:
            settings = settings_provider()
            try:
                with scb_factory(settings) as scb:
                    for name in missing:
                        space.save_code_table(name, scb.code_table(name))  # type: ignore[arg-type]
            except ScbError as exc:
                return {}, str(exc)
        for name in FORM_TABLES:
            tables[name] = space.code_table(name) or {}
        return tables, ""

    @app.get("/screening", response_class=HTMLResponse)
    def screenings(request: Request) -> HTMLResponse:
        return page(request, "screenings.html", "screening", screenings=space.screenings())

    @app.get("/screening/ny", response_class=HTMLResponse)
    def new_screening(request: Request) -> HTMLResponse:
        tables, error = code_tables()
        sni = sorted(
            (code, text)
            for code, text in tables.get("naringsgrenkoder", {}).items()
            if len(code) == 5 and code != "00000"
        )
        return page(
            request,
            "screening_new.html",
            "screening",
            tables=tables,
            sni=sni,
            error=error,
            top_default=TOP_DEFAULT,
        )

    @app.post("/screening")
    def start_screening(
        request: Request,
        sni: Annotated[str, Form()] = "",
        oms_kl: Annotated[list[str] | None, Form()] = None,
        anst_kl: Annotated[list[str] | None, Form()] = None,
        lan: Annotated[list[str] | None, Form()] = None,
        topp: Annotated[int, Form()] = TOP_DEFAULT,
        med_bokslut: Annotated[str | None, Form()] = None,
    ) -> Response:
        codes = [c.strip() for c in sni.replace(";", ",").split(",") if c.strip()]
        codes = [c.split()[0] for c in codes]
        if not codes or not all(c.isdigit() and len(c) == 5 for c in codes):
            tables, error = code_tables()
            return page(
                request,
                "screening_new.html",
                "screening",
                tables=tables,
                sni=sorted(
                    (k, v)
                    for k, v in tables.get("naringsgrenkoder", {}).items()
                    if len(k) == 5 and k != "00000"
                ),
                error=error or "Ange minst en SNI-kod med fem siffror, till exempel 62100.",
                top_default=TOP_DEFAULT,
            )
        criteria = ScreeningCriteria(
            sni=codes,
            turnover_classes=sorted(set(oms_kl or []), key=int),
            employee_classes=sorted(set(anst_kl or []), key=int),
            counties=sorted({c.zfill(2) for c in lan or []}),
        )
        top = max(0, min(topp, 100)) if med_bokslut is not None else 0
        item_id = space.new_id()
        settings = settings_provider()

        def work(report: Reporter) -> str:
            with ExitStack() as clients:
                scb = clients.enter_context(scb_factory(settings))
                registry = (
                    clients.enter_context(bolagsverket_factory(settings)) if top > 0 else None
                )
                result = run_screening(
                    criteria,
                    scb,
                    registry,
                    top=top,
                    progress=report.stage,
                    on_report=lambda done, total: report.detail(
                        f"{done} av {total} årsredovisningar"
                    ),
                )
            report.stage(SCREEN_SAVE_STAGE)
            for name, table in result.labels.items():
                with contextlib.suppress(KeyError):
                    space.save_code_table(name, table)
            meta = ScreeningMeta(
                id=item_id,
                created=dt.datetime.now(),
                title=_criteria_title(criteria, result.labels),
                candidates=len(result.candidates),
                reports=result.reports_fetched,
            )
            space.save_screening(meta, result)
            export_long_list(result, space.screening_dir(item_id))
            return f"/screening/{item_id}"

        job = jobs.start(
            "screening",
            "Screening " + ", ".join(codes),
            [*SCREENING_STAGES, SCREEN_SAVE_STAGE],
            work,
            back="/screening/ny",
        )
        return RedirectResponse(f"/jobb/{job.id}", status_code=303)

    def screening_or_404(item_id: str) -> tuple[ScreeningMeta, Any]:
        try:
            return space.screening(item_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, "Screeningen finns inte.") from exc

    @app.get("/screening/{item_id}", response_class=HTMLResponse)
    def screening(request: Request, item_id: str) -> HTMLResponse:
        meta, result = screening_or_404(item_id)
        rows = views.candidate_rows(result)
        shown = request.query_params.get("visa", "sokta")
        visible = [r for r in rows if r.looked_up] if shown == "sokta" else rows
        if not visible:
            visible, shown = rows, "alla"
        return page(
            request,
            "screening.html",
            "screening",
            meta=meta,
            r=result,
            rows=visible,
            shown=shown,
            total=len(rows),
            looked_up=sum(1 for row in rows if row.looked_up),
            missing=sum(1 for row in rows if row.status == "Saknas digitalt"),
            unanswered=sum(1 for row in rows if row.status == UNANSWERED),
        )

    @app.post("/screening/{item_id}/komplettera")
    def complete_screening(item_id: str) -> Response:
        """Look up again the annual reports Bolagsverket did not answer."""
        meta, result = screening_or_404(item_id)
        settings = settings_provider()

        def work(report: Reporter) -> str:
            report.stage(RETRY_STAGE)
            with bolagsverket_factory(settings) as registry:
                updated = retry_unavailable(
                    result,
                    registry,
                    on_report=lambda done, total: report.detail(
                        f"{done} av {total} årsredovisningar"
                    ),
                )
            report.stage(SCREEN_SAVE_STAGE)
            space.save_screening(meta, updated)
            export_long_list(updated, space.screening_dir(item_id))
            return f"/screening/{item_id}"

        job = jobs.start(
            "screening",
            "Kompletterar screeningen",
            [RETRY_STAGE, SCREEN_SAVE_STAGE],
            work,
            back=f"/screening/{item_id}",
        )
        return RedirectResponse(f"/jobb/{job.id}", status_code=303)

    @app.get("/screening/{item_id}/bolag/{org_nr}", response_class=HTMLResponse)
    def company(request: Request, item_id: str, org_nr: str) -> HTMLResponse:
        meta, result = screening_or_404(item_id)
        candidate = next((c for c in result.candidates if c.org_nr == org_nr), None)
        if candidate is None:
            raise HTTPException(404, "Bolaget finns inte i screeningen.")
        row = next(r for r in views.candidate_rows(result) if r.org_nr == org_nr)
        report, error = None, ""
        if request.query_params.get("bokslut") == "1":
            try:
                with bolagsverket_factory(settings_provider()) as registry:
                    report = registry.latest_report(org_nr)
                if report is None:
                    error = "Bolaget har ingen digital årsredovisning hos Bolagsverket."
            except BolagsverketError as exc:
                error = str(exc)
        figure_rows = [
            ("Nettoomsättning", "nettoomsattning", "tsek"),
            ("Rörelseresultat", "rorelseresultat", "tsek"),
            ("Resultat efter finansiella poster", "resultat_efter_finansiella", "tsek"),
            ("Årets resultat", "arets_resultat", "tsek"),
            ("Eget kapital", "eget_kapital", "tsek"),
            ("Summa tillgångar", "tillgangar", "tsek"),
            ("Soliditet", "soliditet", "pct"),
            ("Medelantal anställda", "medelantal_anstallda", "antal"),
        ]
        return page(
            request,
            "company.html",
            "screening",
            meta=meta,
            r=result,
            c=candidate,
            row=row,
            report=report,
            error=error,
            figure_rows=figure_rows,
        )

    @app.get("/screening/{item_id}/fil/long_list.xlsx")
    def download_long_list(item_id: str) -> FileResponse:
        meta, _ = screening_or_404(item_id)
        return FileResponse(
            space.screening_dir(item_id) / "long_list.xlsx",
            filename=f"Long list {meta.created:%Y-%m-%d}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.post("/screening/{item_id}/ta-bort")
    def delete_screening(item_id: str) -> Response:
        screening_or_404(item_id)
        space.delete_screening(item_id)
        return RedirectResponse("/screening", status_code=303)

    # -------------------------------------------------------------------- jobs

    @app.get("/jobb/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> Response:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Körningen finns inte längre. Starta den igen.")
        if job.status == "klar":
            return RedirectResponse(job.target, status_code=303)
        nav = "screening" if job.kind == "screening" else "analyser"
        return page(request, "job.html", nav, job=job)

    @app.get("/api/jobb/{job_id}")
    def job_status(job_id: str) -> JSONResponse:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Körningen finns inte.")
        return JSONResponse(
            {
                "status": job.status,
                "current": job.current,
                "detail": job.detail,
                "error": job.error,
                "target": job.target,
                "seconds": round(job.seconds, 1),
            }
        )

    # ---------------------------------------------------------------- settings

    @app.get("/installningar", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        cached = {name: space.code_table(name) is not None for name in FORM_TABLES}
        return page(
            request,
            "settings.html",
            "installningar",
            workspace=space.root.resolve(),
            cached=cached,
        )

    @app.post("/api/test/{service}")
    def test_connection(service: str) -> JSONResponse:
        settings = settings_provider()
        try:
            if service == "scb":
                with scb_factory(settings) as scb:
                    scb.code_table("lankoder")
                return JSONResponse({"ok": True, "message": "SCB svarar."})
            if service == "bolagsverket":
                with bolagsverket_factory(settings) as registry:
                    ok = registry.alive()
                message = "Bolagsverket svarar." if ok else "Bolagsverket svarade inte OK."
                return JSONResponse({"ok": ok, "message": message})
        except (ScbError, BolagsverketError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)})
        raise HTTPException(404, "Okänd tjänst.")

    @app.post("/installningar/kodtabeller")
    def refresh_tables() -> Response:
        code_tables(refresh=True)
        return RedirectResponse("/installningar", status_code=303)

    @app.exception_handler(HTTPException)
    async def not_found(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request,
            "error.html",
            {"nav": "", "status": status(), "code": exc.status_code, "message": exc.detail},
            status_code=exc.status_code,
        )

    return app

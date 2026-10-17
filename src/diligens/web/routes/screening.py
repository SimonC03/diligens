"""Buy & Build screening: the form, the run, the long list, company pages and retries."""

import datetime as dt
from contextlib import ExitStack
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from diligens.export.longlist import export_long_list
from diligens.models import ScreeningCriteria
from diligens.screening import (
    SCREENING_STAGES,
    TOP_DEFAULT,
    UNANSWERED,
    retry_unavailable,
    run_screening,
)
from diligens.sources.bolagsverket import BolagsverketError
from diligens.web import views
from diligens.web.context import Ctx, CtxDep
from diligens.web.jobs import Reporter
from diligens.web.routes.general import code_tables
from diligens.web.workspace import ScreeningMeta

router = APIRouter()

SAVE_STAGE = "Sparar long list"
RETRY_STAGE = "Hämtar återstående årsredovisningar"
REPORT_STAGE = SCREENING_STAGES[-1]
MAX_TOP = 100
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FIGURE_ROWS = [
    ("Nettoomsättning", "nettoomsattning", "tsek"),
    ("Rörelseresultat", "rorelseresultat", "tsek"),
    ("Resultat efter finansiella poster", "resultat_efter_finansiella", "tsek"),
    ("Årets resultat", "arets_resultat", "tsek"),
    ("Eget kapital", "eget_kapital", "tsek"),
    ("Summa tillgångar", "tillgangar", "tsek"),
    ("Soliditet", "soliditet", "pct"),
    ("Medelantal anställda", "medelantal_anstallda", "antal"),
]


def _criteria_title(criteria: ScreeningCriteria, labels: dict[str, dict[str, str]]) -> str:
    parts = ["SNI " + ", ".join(criteria.sni)]
    if criteria.turnover_classes:
        parts.append("oms.klass " + ", ".join(criteria.turnover_classes))
    if criteria.employee_classes:
        parts.append("anst.klass " + ", ".join(criteria.employee_classes))
    if criteria.counties:
        names = labels.get("lankoder", {})
        parts.append(", ".join(names.get(c, c) for c in criteria.counties))
    return " · ".join(parts)


def _form(request: Request, ctx: Ctx, chosen: dict[str, Any], error: str = "") -> HTMLResponse:
    tables, table_error = code_tables(ctx)
    sni = sorted(
        (code, text)
        for code, text in tables.get("naringsgrenkoder", {}).items()
        if len(code) == 5 and code != "00000"
    )
    return ctx.page(
        request,
        "screening_new.html",
        "screening",
        tables=tables,
        sni=sni,
        error=table_error or error,
        chosen=chosen,
    )


def _codes(values: list[str] | None, width: int = 0) -> list[str]:
    """Distinct numeric codes in order, zero padded to `width`; anything else is dropped."""
    out: list[str] = []
    for value in values or []:
        code = value.strip().zfill(width) if width else value.strip()
        if code.isdigit() and code not in out:
            out.append(code)
    return sorted(out, key=int)


@router.get("/screening", response_class=HTMLResponse)
def screenings(request: Request, ctx: CtxDep) -> HTMLResponse:
    return ctx.page(request, "screenings.html", "screening", screenings=ctx.space.screenings())


@router.get("/screening/ny", response_class=HTMLResponse)
def new_screening(request: Request, ctx: CtxDep) -> HTMLResponse:
    chosen = {"sni": [], "oms": [], "anst": [], "lan": [], "topp": TOP_DEFAULT, "bokslut": True}
    return _form(request, ctx, chosen)


@router.post("/screening")
def start_screening(
    request: Request,
    ctx: CtxDep,
    sni: Annotated[str, Form()] = "",
    oms_kl: Annotated[list[str] | None, Form()] = None,
    anst_kl: Annotated[list[str] | None, Form()] = None,
    lan: Annotated[list[str] | None, Form()] = None,
    topp: Annotated[str, Form()] = str(TOP_DEFAULT),
    med_bokslut: Annotated[str | None, Form()] = None,
) -> Response:
    entered = [part.strip().split()[0] for part in sni.replace(";", ",").split(",") if part.strip()]
    codes = list(dict.fromkeys(entered))
    top = int(topp) if topp.strip().isdigit() else TOP_DEFAULT
    top = max(1, min(top, MAX_TOP)) if med_bokslut is not None else 0
    chosen = {
        "sni": codes,
        "oms": _codes(oms_kl),
        "anst": _codes(anst_kl),
        "lan": _codes(lan, 2),
        "topp": top or TOP_DEFAULT,
        "bokslut": med_bokslut is not None,
    }
    if not codes or not all(c.isdigit() and len(c) == 5 for c in codes):
        return _form(
            request, ctx, chosen, "Ange minst en SNI-kod med fem siffror, till exempel 62100."
        )
    criteria = ScreeningCriteria(
        sni=codes,
        turnover_classes=chosen["oms"],
        employee_classes=chosen["anst"],
        counties=chosen["lan"],
    )
    item_id = ctx.space.new_id()
    settings = ctx.settings
    space = ctx.space

    def work(report: Reporter) -> str:
        with ExitStack() as clients:
            scb = clients.enter_context(ctx.scb_factory(settings))
            registry = (
                clients.enter_context(ctx.bolagsverket_factory(settings)) if top > 0 else None
            )
            result = run_screening(
                criteria,
                scb,
                registry,
                top=top,
                progress=report.stage,
                on_report=lambda done, total: report.detail(f"{done} av {total} årsredovisningar"),
            )
        report.stage(SAVE_STAGE)
        for name, table in result.labels.items():
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

    stages = [s for s in SCREENING_STAGES if top > 0 or s != REPORT_STAGE]
    job = ctx.jobs.start(
        "screening",
        "Screening " + ", ".join(codes),
        [*stages, SAVE_STAGE],
        work,
        back="/screening/ny",
    )
    return RedirectResponse(f"/jobb/{job.id}", status_code=303)


@router.get("/screening/{item_id}", response_class=HTMLResponse)
def screening(request: Request, item_id: str, ctx: CtxDep) -> HTMLResponse:
    meta, result = ctx.screening(item_id)
    rows = views.candidate_rows(result)
    shown = request.query_params.get("visa", "sokta")
    visible = [r for r in rows if r.looked_up] if shown == "sokta" else rows
    if not visible:
        visible, shown = rows, "alla"
    return ctx.page(
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


@router.post("/screening/{item_id}/komplettera")
def complete_screening(item_id: str, ctx: CtxDep) -> Response:
    """Look up again the annual reports Bolagsverket did not answer, and rank again."""
    meta, result = ctx.screening(item_id)
    settings = ctx.settings
    space = ctx.space

    def work(report: Reporter) -> str:
        report.stage(RETRY_STAGE)
        with ctx.bolagsverket_factory(settings) as registry:
            updated = retry_unavailable(
                result,
                registry,
                on_report=lambda done, total: report.detail(f"{done} av {total} årsredovisningar"),
            )
        report.stage(SAVE_STAGE)
        space.save_screening(meta, updated)
        export_long_list(updated, space.screening_dir(item_id))
        return f"/screening/{item_id}"

    job = ctx.jobs.start(
        "screening",
        "Kompletterar screeningen",
        [RETRY_STAGE, SAVE_STAGE],
        work,
        back=f"/screening/{item_id}",
    )
    return RedirectResponse(f"/jobb/{job.id}", status_code=303)


@router.get("/screening/{item_id}/bolag/{org_nr}", response_class=HTMLResponse)
def company(request: Request, item_id: str, org_nr: str, ctx: CtxDep) -> HTMLResponse:
    meta, result = ctx.screening(item_id)
    candidate = next((c for c in result.candidates if c.org_nr == org_nr), None)
    if candidate is None:
        raise HTTPException(404, "Bolaget finns inte i screeningen.")
    row = next(r for r in views.candidate_rows(result) if r.org_nr == org_nr)
    report, error = None, ""
    if request.query_params.get("bokslut") == "1":
        try:
            with ctx.bolagsverket_factory(ctx.settings) as registry:
                report = registry.latest_report(org_nr)
            if report is None:
                error = "Bolaget har ingen digital årsredovisning hos Bolagsverket."
        except BolagsverketError as exc:
            error = str(exc)
    return ctx.page(
        request,
        "company.html",
        "screening",
        meta=meta,
        r=result,
        c=candidate,
        row=row,
        report=report,
        error=error,
        figure_rows=FIGURE_ROWS,
    )


@router.get("/screening/{item_id}/fil/long_list.xlsx")
def download_long_list(item_id: str, ctx: CtxDep) -> FileResponse:
    meta, _ = ctx.screening(item_id)
    return FileResponse(
        ctx.space.screening_dir(item_id) / "long_list.xlsx",
        filename=f"Long list {meta.created:%Y-%m-%d}.xlsx",
        media_type=XLSX,
    )


@router.post("/screening/{item_id}/ta-bort")
def delete_screening(item_id: str, ctx: CtxDep) -> Response:
    ctx.screening(item_id)
    ctx.space.delete_screening(item_id)
    return RedirectResponse("/screening", status_code=303)

"""Due diligence: upload, run, the result tabs, the advisor's choices and downloads."""

import datetime as dt
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask

from diligens.agent.client import make_client
from diligens.agent.memo import write_memo
from diligens.analysis.bridge import with_selection
from diligens.export.excel import export_dd_pack
from diligens.pipeline import ANALYSIS_STAGES, run_analysis
from diligens.web import views
from diligens.web.context import Ctx, CtxDep
from diligens.web.jobs import Reporter
from diligens.web.security import MAX_UPLOAD, TOO_LARGE
from diligens.web.workspace import AnalysisMeta

router = APIRouter()

SAVE_STAGE = "Sparar och skapar Excel"
MEMO_STAGE = "Skriver memo"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class Selection(BaseModel):
    inkludera: list[bool]


def _form(request: Request, ctx: Ctx, error: str) -> HTMLResponse:
    return ctx.page(request, "analysis_new.html", "analyser", demo=ctx.demo_available, error=error)


@router.get("/analyser", response_class=HTMLResponse)
def analyses(request: Request, ctx: CtxDep) -> HTMLResponse:
    return ctx.page(request, "analyses.html", "analyser", analyses=ctx.space.analyses())


@router.get("/analyser/ny", response_class=HTMLResponse)
def new_analysis(request: Request, ctx: CtxDep) -> HTMLResponse:
    return ctx.page(request, "analysis_new.html", "analyser", demo=ctx.demo_available)


@router.post("/analyser")
async def start_analysis(
    request: Request,
    ctx: CtxDep,
    fil: Annotated[UploadFile | None, File()] = None,
    med_ai: Annotated[str | None, Form()] = None,
    narstaende: Annotated[str, Form()] = "",
    demo: Annotated[str | None, Form()] = None,
) -> Response:
    space = ctx.space
    item_id = space.new_id()
    target = space.upload_path(item_id)
    if demo and ctx.demo_file is not None and ctx.demo_available:
        target.write_bytes(ctx.demo_file.read_bytes())
        source_name = ctx.demo_file.name
    elif fil is not None and fil.filename:
        size = 0
        with target.open("wb") as out:
            while chunk := await fil.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    break
                out.write(chunk)
        if size > MAX_UPLOAD:
            space.delete_analysis(item_id)
            return _form(request, ctx, TOO_LARGE)
        source_name = Path(fil.filename).name
    else:
        space.delete_analysis(item_id)
        return _form(request, ctx, "Välj en SIE4-fil att analysera.")

    use_llm = med_ai is not None
    related = [line.strip() for line in narstaende.splitlines() if line.strip()]
    settings = ctx.settings

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
        except BaseException:
            space.delete_analysis(item_id)  # no half saved client file left behind
            raise
        return f"/analyser/{item_id}"

    job = ctx.jobs.start(
        "analys",
        f"Analyserar {source_name}",
        [*ANALYSIS_STAGES, SAVE_STAGE],
        work,
        back="/analyser/ny",
    )
    return RedirectResponse(f"/jobb/{job.id}", status_code=303)


@router.get("/analyser/{item_id}")
def analysis_home(item_id: str, ctx: CtxDep) -> Response:
    ctx.analysis_meta(item_id)
    return RedirectResponse(f"/analyser/{item_id}/sammanfattning", status_code=307)


@router.get("/analyser/{item_id}/verifikat/{ref}", response_class=HTMLResponse)
def voucher(request: Request, item_id: str, ref: str, ctx: CtxDep) -> HTMLResponse:
    """One voucher as booked, read from the uploaded SIE file."""
    meta, result = ctx.analysis(item_id)
    view = views.voucher_view(ctx.space.ledger(item_id), ref)
    if view is None:
        raise HTTPException(404, "Verifikatet finns inte i SIE-filen.")
    flagged = next((r for r in views.flag_rows(result) if r.flag.voucher_ref == ref), None)
    fragment = request.query_params.get("del") == "1"
    return ctx.page(
        request,
        "_voucher.html" if fragment else "voucher.html",
        "analyser",
        meta=meta,
        voucher=view,
        flagged=flagged,
    )


@router.get("/analyser/{item_id}/{tab}", response_class=HTMLResponse)
def analysis_tab(request: Request, item_id: str, tab: str, ctx: CtxDep) -> HTMLResponse:
    if tab not in views.TAB_NAMES:
        raise HTTPException(404, "Fliken finns inte.")
    meta, result = ctx.analysis(item_id)
    context: dict[str, Any] = {
        "meta": meta,
        "r": result,
        "tab": tab,
        "tabs": views.TABS,
        "tab_name": views.TAB_NAMES[tab],
        "months": result.statements.months,
        **views.tab_context(tab, result, request.query_params.get("status", "")),
    }
    return ctx.page(request, f"tab_{tab}.html", "analyser", **context)


@router.post("/api/analyser/{item_id}/urval")
def select(item_id: str, body: Selection, ctx: CtxDep) -> JSONResponse:
    """Save the advisor's Inkludera choices; the totals come from with_selection."""
    ctx.analysis_meta(item_id)
    original = ctx.space.proposed(item_id).bridge
    proposed = [i.included for i in [*original.items, *original.candidates]]
    try:
        bridge = with_selection(original, body.inkludera)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    selection = None if body.inkludera == proposed else body.inkludera
    meta = ctx.space.update_meta(item_id, selection=selection)
    return JSONResponse(
        {
            "justerad_ebitda": views.tsek(bridge.adjusted),
            "justerad_omsattning": views.tsek(bridge.adjusted_revenue),
            "marginal": views.percent(views.adjusted_margin(bridge)),
            "memo_aktuellt": meta.memo_current,
            "andrat": selection is not None,
        }
    )


@router.post("/analyser/{item_id}/memo")
def rewrite_memo(item_id: str, ctx: CtxDep) -> Response:
    meta, result = ctx.analysis(item_id)
    settings = ctx.settings
    space = ctx.space

    def work(report: Reporter) -> str:
        report.stage(MEMO_STAGE)
        client = make_client(settings) if meta.use_llm and settings.llm_enabled else None
        memo = write_memo(result, client, model=meta.model or settings.diligens_model)
        space.save_memo(item_id, memo)
        # The memo matches the selection it was written with, even if it changed meanwhile.
        space.update_meta(item_id, memo_selection=meta.selection)
        return f"/analyser/{item_id}/memo"

    job = ctx.jobs.start(
        "memo",
        f"Memo för {meta.company_name}",
        [MEMO_STAGE],
        work,
        back=f"/analyser/{item_id}/memo",
    )
    return RedirectResponse(f"/jobb/{job.id}", status_code=303)


@router.get("/analyser/{item_id}/fil/dd_pack.xlsx")
def download_pack(item_id: str, ctx: CtxDep) -> FileResponse:
    """The Excel pack with the advisor's current choices, written fresh for each download."""
    meta, result = ctx.analysis(item_id)
    folder = Path(tempfile.mkdtemp(prefix="diligens-"))
    path = export_dd_pack(result, folder)
    return FileResponse(
        path,
        filename=f"{meta.company_name} DD-underlag.xlsx",
        media_type=XLSX,
        background=BackgroundTask(shutil.rmtree, folder, ignore_errors=True),
    )


@router.get("/analyser/{item_id}/fil/memo.md")
def download_memo(item_id: str, ctx: CtxDep) -> FileResponse:
    meta = ctx.analysis_meta(item_id)
    return FileResponse(
        ctx.space.analysis_dir(item_id) / "memo.md",
        filename=f"{meta.company_name} memo.md",
        media_type="text/markdown; charset=utf-8",
    )


@router.post("/analyser/{item_id}/ta-bort")
def delete_analysis(item_id: str, ctx: CtxDep) -> Response:
    ctx.analysis_meta(item_id)
    ctx.space.delete_analysis(item_id)
    return RedirectResponse("/analyser", status_code=303)

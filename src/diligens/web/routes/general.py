"""Overview, method, running jobs and settings."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from diligens.export.excel import CATEGORY_LABELS
from diligens.models import FlagCategory
from diligens.sources.bolagsverket import BolagsverketError
from diligens.sources.scb import CodeTable, ScbError
from diligens.web.context import Ctx, CtxDep

router = APIRouter()

FORM_TABLES: tuple[CodeTable, ...] = ("naringsgrenkoder", "omsklkoder", "anstklkoder", "lankoder")

CATEGORY_TEXT: list[tuple[FlagCategory, str]] = [
    (
        "engangspost",
        "Engångsposter, till exempel flytt, tvist eller rekrytering. Läggs tillbaka i "
        "justerad EBITDA.",
    ),
    (
        "agarrelaterad",
        "Kostnader i ägarens sfär, till exempel privat bil eller arvode till ägarbolaget. "
        "Läggs tillbaka i sin helhet.",
    ),
    ("periodisering", "Intäkt eller kostnad bokförd i fel period. Flyttas ut ur perioden."),
    (
        "narstaende",
        "Transaktion med närstående part. Listas med Inkludera av, rådgivaren bedömer villkoren.",
    ),
    ("avvikelse", "Ovanligt mönster som är värt en fråga men inte en justering."),
]


def code_tables(ctx: Ctx, refresh: bool = False) -> tuple[dict[str, dict[str, str]], str]:
    """Code tables for the screening form, from the workspace cache or SCB; an error text
    when neither has them."""
    missing = [n for n in FORM_TABLES if refresh or ctx.space.code_table(n) is None]
    if missing:
        try:
            with ctx.scb_factory(ctx.settings) as scb:
                for name in missing:
                    ctx.space.save_code_table(name, scb.code_table(name))
        except ScbError as exc:
            return {}, str(exc)
    return {name: ctx.space.code_table(name) or {} for name in FORM_TABLES}, ""


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, ctx: CtxDep) -> HTMLResponse:
    analyses = ctx.space.analyses()
    screenings = ctx.space.screenings()
    recent: list[dict[str, Any]] = [
        {
            "kind": "Due diligence",
            "title": a.company_name,
            "detail": f"{a.org_nr} · {a.basis_label} {a.period}",
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
    return ctx.page(
        request,
        "overview.html",
        "oversikt",
        recent=recent[:8],
        counts={"analyses": len(analyses), "screenings": len(screenings)},
    )


@router.get("/metod", response_class=HTMLResponse)
def method(request: Request, ctx: CtxDep) -> HTMLResponse:
    categories = [(CATEGORY_LABELS[key], text) for key, text in CATEGORY_TEXT]
    return ctx.page(request, "method.html", "metod", categories=categories)


@router.get("/jobb/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str, ctx: CtxDep) -> Response:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Körningen finns inte längre, till exempel efter en omstart.")
    if job.status == "klar":
        return RedirectResponse(job.target, status_code=303)
    nav = "screening" if job.kind == "screening" else "analyser"
    return ctx.page(request, "job.html", nav, job=job)


@router.get("/api/jobb/{job_id}")
def job_status(job_id: str, ctx: CtxDep) -> JSONResponse:
    job = ctx.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Körningen finns inte längre. Starta den igen.")
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


@router.get("/installningar", response_class=HTMLResponse)
def settings_page(request: Request, ctx: CtxDep, error: str = "") -> HTMLResponse:
    cached = {name: ctx.space.code_table(name) is not None for name in FORM_TABLES}
    return ctx.page(
        request,
        "settings.html",
        "installningar",
        workspace=ctx.space.root.resolve(),
        cached=cached,
        error=error,
    )


@router.post("/installningar/kodtabeller")
def refresh_tables(request: Request, ctx: CtxDep) -> Response:
    _, error = code_tables(ctx, refresh=True)
    if error:
        return settings_page(request, ctx, error=error)
    return RedirectResponse("/installningar", status_code=303)


@router.post("/api/test/{service}")
def test_connection(service: str, ctx: CtxDep) -> JSONResponse:
    try:
        if service == "scb":
            with ctx.scb_factory(ctx.settings) as scb:
                scb.code_table("lankoder")
            return JSONResponse({"ok": True, "message": "SCB svarar."})
        if service == "bolagsverket":
            with ctx.bolagsverket_factory(ctx.settings) as registry:
                ok = registry.alive()
            message = "Bolagsverket svarar." if ok else "Bolagsverket svarade inte OK."
            return JSONResponse({"ok": ok, "message": message})
    except (ScbError, BolagsverketError) as exc:
        return JSONResponse({"ok": False, "message": str(exc)})
    raise HTTPException(404, "Okänd tjänst.")

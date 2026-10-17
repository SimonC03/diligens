"""The Buy & Build screening run. The CLI and the MCP server both call run_screening."""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from diligens.analysis.screening import financial_score, select_candidates
from diligens.models import Candidate, FinancialScore, ScbUnit, ScreeningCriteria, ScreeningResult
from diligens.sources.bolagsverket import BolagsverketClient, BolagsverketError
from diligens.sources.scb import ScbClient

TOP_DEFAULT = 25
WORKERS = 4
MAX_TURNOVER_CLASS = 12
NOT_FETCHED = FinancialScore(
    points=None, reasons=["Årsredovisningen hämtades inte (utanför topplistan)"], status="Ej hämtad"
)


def _turnover_classes(targets: list[str]) -> list[str]:
    """Target classes and their neighbours, since one class off still qualifies."""
    wanted = {int(t) + d for t in targets for d in (-1, 0, 1)}
    return [str(c) for c in sorted(wanted) if 0 <= c <= MAX_TURNOVER_CLASS]


def run_screening(
    criteria: ScreeningCriteria,
    scb: ScbClient,
    bolagsverket: BolagsverketClient | None,
    top: int = TOP_DEFAULT,
) -> ScreeningResult:
    """SCB candidates with a fit score; annual reports for the `top` best profiles."""
    units: dict[str, ScbUnit] = {}
    for sni in criteria.sni:
        for unit in scb.legal_units("naringsgren", sni):
            units[unit.pe_org_nr] = unit

    turnover: dict[str, str] = {}
    for turnover_class in _turnover_classes(criteria.turnover_classes):
        for unit in scb.legal_units("omsattningsklass", turnover_class):
            if unit.pe_org_nr in units:
                turnover[unit.pe_org_nr] = turnover_class

    candidates = select_candidates(list(units.values()), turnover, criteria)
    shortlist = candidates[:top] if bolagsverket is not None else []

    def score(candidate: Candidate) -> Candidate:
        assert bolagsverket is not None
        try:
            financial = financial_score(bolagsverket.latest_report(candidate.org_nr))
        except BolagsverketError as exc:
            financial = FinancialScore(points=None, reasons=[str(exc)], status="Kunde inte hämtas")
        return candidate.model_copy(update={"financial": financial})

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        scored = list(pool.map(score, shortlist))
    rest = [c.model_copy(update={"financial": NOT_FETCHED}) for c in candidates[len(shortlist) :]]
    ranked = sorted(scored + rest, key=lambda c: (-c.total, c.unit.name))

    labels = {name: scb.code_table(name) for name in ("omsklkoder", "anstklkoder", "lankoder")}
    return ScreeningResult(
        criteria=criteria,
        candidates=ranked,
        units_scanned=len(units),
        reports_fetched=len(shortlist),
        labels=labels,
        generated=dt.date.today(),
    )

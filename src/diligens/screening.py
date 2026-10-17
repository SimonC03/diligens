"""The Buy & Build screening run. The CLI and the MCP server both call run_screening."""

import datetime as dt
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from diligens.analysis.screening import financial_score, select_candidates
from diligens.models import Candidate, FinancialScore, ScbUnit, ScreeningCriteria, ScreeningResult
from diligens.sources.bolagsverket import (
    BolagsverketClient,
    BolagsverketError,
    BolagsverketUnavailable,
    ReportNotReadable,
)
from diligens.sources.scb import ScbClient

log = logging.getLogger(__name__)

TOP_DEFAULT = 25
MAX_REPORTS = 100  # ties at the cut-off are included up to this many lookups
WORKERS = 4
MAX_TURNOVER_CLASS = 12
NOT_FETCHED = FinancialScore(
    points=None, reasons=["Årsredovisningen hämtades inte (utanför topplistan)"], status="Ej hämtad"
)


# Stages reported through `progress`, in order.
SCREENING_STAGES = (
    "Hämtar kodtabeller från SCB",
    "Hämtar bolag i vald bransch",
    "Matchar omsättningsklasser",
    "Väljer kandidater",
    "Hämtar årsredovisningar",
)


def _turnover_classes(targets: list[str]) -> list[str]:
    """Target classes and their neighbours, since one class off still qualifies."""
    wanted = {int(t) + d for t in targets for d in (-1, 0, 1)}
    return [str(c) for c in sorted(wanted) if 0 <= c <= MAX_TURNOVER_CLASS]


def _shortlist(candidates: list[Candidate], top: int) -> tuple[list[Candidate], list[str]]:
    """The `top` best profiles plus every candidate tied with the last one, so the cut-off
    never depends on the company name. Capped at MAX_REPORTS, with a note when it bites."""
    if top <= 0 or not candidates:
        return [], []
    cutoff = candidates[min(top, len(candidates)) - 1].profile_points
    tied = [c for c in candidates if c.profile_points >= cutoff]
    notes = []
    if len(tied) > top:
        notes.append(
            f"{len(tied)} bolag delar profilpoängen vid gränsen; årsredovisning söks för alla "
            "så att ordningen inte beror på namnet"
        )
    if len(tied) > MAX_REPORTS:
        notes.append(
            f"Bara {MAX_REPORTS} av {len(tied)} jämbördiga bolag fick årsredovisningen sökt. "
            "Snäva in kriterierna för en rättvis topplista."
        )
        tied = tied[:MAX_REPORTS]
    return tied, notes


def with_ranks(candidates: list[Candidate]) -> list[tuple[int | None, Candidate]]:
    """Rank 1, 2, ... for companies whose annual report was looked up; None for the rest,
    which follow unranked since their financial score is unknown."""
    out: list[tuple[int | None, Candidate]] = []
    rank = 0
    for c in candidates:
        looked_up = c.financial is not None and c.financial.status != NOT_FETCHED.status
        rank += looked_up
        out.append((rank if looked_up else None, c))
    return out


def run_screening(
    criteria: ScreeningCriteria,
    scb: ScbClient,
    bolagsverket: BolagsverketClient | None,
    top: int = TOP_DEFAULT,
    today: dt.date | None = None,
    progress: Callable[[str], None] | None = None,
    on_report: Callable[[int, int], None] | None = None,
) -> ScreeningResult:
    """SCB candidates with a fit score; annual reports for the best profiles.

    `progress` is called with each of SCREENING_STAGES as it starts, `on_report` with
    (done, total) after each annual report lookup.
    """
    today = today or dt.date.today()
    stage = iter(SCREENING_STAGES)

    def step() -> None:
        name = next(stage)
        if progress is not None:
            progress(name)

    step()
    # Code tables first: an SCB failure here should not waste the slow annual report lookups.
    labels = {name: scb.code_table(name) for name in ("omsklkoder", "anstklkoder", "lankoder")}

    step()
    units: dict[str, ScbUnit] = {}
    for sni in criteria.sni:
        for unit in scb.legal_units("naringsgren", sni):
            units[unit.pe_org_nr] = unit

    step()
    turnover: dict[str, str] = {}
    for turnover_class in _turnover_classes(criteria.turnover_classes):
        for unit in scb.legal_units("omsattningsklass", turnover_class):
            if unit.pe_org_nr in units:
                turnover[unit.pe_org_nr] = turnover_class

    step()
    candidates = select_candidates(list(units.values()), turnover, criteria)
    shortlist, notes = _shortlist(candidates, top) if bolagsverket is not None else ([], [])
    unavailable = threading.Event()
    done = 0
    counter = threading.Lock()

    def counted(candidate: Candidate) -> Candidate:
        nonlocal done
        scored = score(candidate)
        with counter:
            done += 1
            if on_report is not None:
                on_report(done, len(shortlist))
        return scored

    def score(candidate: Candidate) -> Candidate:
        assert bolagsverket is not None
        if unavailable.is_set():
            financial = FinancialScore(
                points=None,
                reasons=["Bolagsverket svarade inte, årsredovisningen söktes inte"],
                status="Kunde inte hämtas",
            )
            return candidate.model_copy(update={"financial": financial})
        try:
            financial = financial_score(bolagsverket.latest_report(candidate.org_nr), today)
        except ReportNotReadable as exc:
            financial = FinancialScore(points=None, reasons=[str(exc)], status="Kunde inte läsas")
        except BolagsverketUnavailable as exc:
            unavailable.set()
            financial = FinancialScore(points=None, reasons=[str(exc)], status="Kunde inte hämtas")
        except BolagsverketError as exc:
            financial = FinancialScore(points=None, reasons=[str(exc)], status="Kunde inte hämtas")
        except Exception:  # one odd company must never stop the whole screening
            log.exception("Årsredovisning för %s kunde inte behandlas", candidate.org_nr)
            financial = FinancialScore(
                points=None,
                reasons=["Oväntat fel när årsredovisningen behandlades"],
                status="Kunde inte läsas",
            )
        return candidate.model_copy(update={"financial": financial})

    step()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        scored = list(pool.map(counted, shortlist))
    if unavailable.is_set():
        notes.append("Bolagsverket svarade inte; försök igen senare för fler årsredovisningar")
    fetched = {c.org_nr for c in shortlist}
    rest = [
        c.model_copy(update={"financial": NOT_FETCHED})
        for c in candidates
        if c.org_nr not in fetched
    ]
    # Looked-up companies rank by total score; the rest follow by profile, unranked.
    ranked = sorted(scored, key=lambda c: (-c.total, c.unit.name)) + rest

    return ScreeningResult(
        criteria=criteria,
        candidates=ranked,
        units_scanned=len(units),
        reports_fetched=len(shortlist),
        labels=labels,
        notes=notes,
        generated=today,
    )

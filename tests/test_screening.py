"""M6 screening tests: filters and fit score (pure) and the full run with fake APIs."""

import datetime as dt
from decimal import Decimal

from diligens.analysis.screening import financial_score, profile_score, select_candidates
from diligens.models import AnnualReport, Figure, ScbUnit, ScreeningCriteria


def _unit(
    n: int,
    *,
    anst: str = "5",
    lan: str = "01",
    jur: str = "49",
    stat: str = "1",
    sni: str = "62100",
) -> ScbUnit:
    return ScbUnit(
        peOrgNr=f"1655990{n:05d}",
        orgNr=f"55990{n:05d}",
        namn=f"Testbolag {n} AB",
        lanSate=lan,
        kommunSate="0180",
        anstKl=anst,
        ftgStat=stat,
        jurform=jur,
        primary_sni=sni,
    )


def _report(revenue: str, prior: str | None, ebit: str) -> AnnualReport:
    figures = {
        "nettoomsattning": Figure(value=Decimal(revenue), source_ref="iXBRL Nettoomsattning"),
        "rorelseresultat": Figure(value=Decimal(ebit), source_ref="iXBRL Rorelseresultat"),
    }
    if prior is not None:
        figures["nettoomsattning_fg"] = Figure(value=Decimal(prior), source_ref="iXBRL fg")
    return AnnualReport(
        document_id="doc",
        org_nr="5599000001",
        period_start=dt.date(2025, 1, 1),
        period_end=dt.date(2025, 12, 31),
        figures=figures,
    )


CRITERIA = ScreeningCriteria(
    sni=["62100"], turnover_classes=["6"], employee_classes=["5"], counties=["01"]
)


def test_filters_and_profile_score() -> None:
    units = [
        _unit(1),  # perfect match
        _unit(2, anst="4", lan="12"),  # adjacent employee class, other county
        _unit(3, jur="10"),  # sole trader: not an aktiebolag
        _unit(4, stat="9"),  # no longer active
        _unit(5, anst="8"),  # far outside the employee class
        _unit(6),  # turnover class adjacent (7)
        _unit(7),  # turnover class far outside (9), dropped
    ]
    turnover = {
        "165599000001": "6",
        "165599000002": "6",
        "165599000003": "6",
        "165599000004": "6",
        "165599000005": "6",
        "165599000006": "7",
        "165599000007": "9",
    }

    candidates = select_candidates(units, turnover, CRITERIA)

    assert [c.org_nr for c in candidates] == ["5599000001", "5599000006", "5599000002"]  # by score
    best = {c.org_nr: c for c in candidates}
    assert best["5599000001"].profile_points == 60
    assert best["5599000002"].profile_points == 15 + 15 + 5 + 0 + 10
    assert best["5599000006"].profile_points == 15 + 7 + 10 + 10 + 10
    reasons = " ".join(best["5599000002"].profile_reasons)
    assert "anställdaklass" in reasons.lower() and "län" in reasons.lower()

    points, reasons = profile_score(_unit(9, lan="03"), "6", ScreeningCriteria(sni=["62100"]))
    assert points == 60  # no size or county targets: nothing to miss
    assert reasons


def test_financial_score_and_missing_reports() -> None:
    strong = financial_score(_report("33000000", "29000000", "3600000"))
    assert strong.points == 40
    assert (
        strong.growth is not None
        and strong.growth.value == Decimal("33000000") / Decimal("29000000") - 1
    )
    assert strong.ebit_margin is not None and "Rorelseresultat" in strong.ebit_margin.source_ref

    middle = financial_score(_report("30000000", "29000000", "2000000"))  # +3 %, 6,7 %
    assert middle.points == 10 + 10
    weak = financial_score(_report("27000000", "29000000", "500000"))  # shrinking, 1,9 %
    assert weak.points == 0
    no_prior = financial_score(_report("27000000", None, "3000000"))
    assert no_prior.points == 20 and no_prior.growth is None
    assert any("föregående år" in r for r in no_prior.reasons)

    missing = financial_score(None)
    assert missing.points is None and missing.status == "Saknas digitalt"

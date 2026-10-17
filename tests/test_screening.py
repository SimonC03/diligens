"""M6 screening tests: filters and fit score (pure) and the full run with fake APIs."""

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from helpers import fake_registry, fake_scb, org
from helpers import unit as scb_unit
from openpyxl import load_workbook
from typer.testing import CliRunner

import diligens.cli as cli
import diligens.screening as screening
from diligens.analysis.screening import financial_score, profile_score, select_candidates
from diligens.cli import app
from diligens.models import AnnualReport, Figure, ScbUnit, ScreeningCriteria
from diligens.screening import SCREENING_STAGES, run_screening
from diligens.sources.bolagsverket import BolagsverketUnavailable, ReportNotReadable
from diligens.sources.scb import ScbClient, ScbError


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
        _unit(9),  # turnover class adjacent (7)
        _unit(7),  # turnover class far outside (9), dropped
    ]
    turnover = {
        "165599000001": "6",
        "165599000002": "6",
        "165599000003": "6",
        "165599000004": "6",
        "165599000005": "6",
        "165599000009": "7",
        "165599000007": "9",
    }

    candidates = select_candidates(units, turnover, CRITERIA)

    assert [c.org_nr for c in candidates] == ["5599000001", "5599000009", "5599000002"]  # by score
    best = {c.org_nr: c for c in candidates}
    assert best["5599000001"].profile_points == 60
    assert best["5599000002"].profile_points == 15 + 15 + 5 + 0 + 10
    assert best["5599000009"].profile_points == 15 + 7 + 10 + 10 + 10
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


def test_screen_writes_long_list_with_sources_and_missing_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = [
        scb_unit(1),
        scb_unit(2, lan="12"),
        scb_unit(3, jur="10"),  # sole trader, filtered out
        scb_unit(4, stat="9"),  # inactive, filtered out
        scb_unit(5, anst="8"),  # too large, filtered out
        scb_unit(9),
    ]
    pe = {n: f"1655990{n:05d}" for n in (1, 2, 3, 4, 5, 9)}
    turnover = {"6": [pe[1], pe[2], pe[3], pe[4], pe[5]], "7": [pe[9]]}
    monkeypatch.setattr(cli, "make_scb_client", lambda s: fake_scb(units, turnover))
    monkeypatch.setattr(cli, "make_bolagsverket_client", lambda s: fake_registry())

    run = CliRunner().invoke(
        app,
        [
            "screen",
            "--sni",
            "62100",
            "--oms-kl",
            "6",
            "--anst-kl",
            "5",
            "--lan",
            "1",
            "--out",
            str(tmp_path),
        ],
    )

    assert run.exit_code == 0, run.output
    assert "3 kandidater av 6 bolag" in run.output
    assert re.search(r"Klart på \d+,\d s", run.output)
    wb = load_workbook(tmp_path / "long_list.xlsx")
    assert wb.sheetnames == ["Long list", "Kriterier"]
    ws = wb["Long list"]
    header = [c.value for c in ws[1]]
    rows = [
        dict(zip(header, (c.value for c in row), strict=True)) for row in ws.iter_rows(min_row=2)
    ]
    assert [r["Org.nr"] for r in rows] == ["559900-0001", "559900-0009", "559900-0002"]

    best = rows[0]
    assert best["Profil"] == 60 and best["Nyckeltal"] == 40  # growth 12,3 %, margin 10,0 %
    assert best["Poäng"] == "=C2+D2"
    assert best["Bokslut"] == "Årsredovisning 2025-12-31" and best["Rang"] == 1
    assert best["Nettoomsättning TSEK"] == pytest.approx(32450.0)
    assert best["Tillväxt"] == pytest.approx(32450 / 28900 - 1)
    assert "iXBRL se-gen-base:Nettoomsattning" in best["Källa"] and "d1_paket" in best["Källa"]
    assert "SCB AFR" in best["Källa"]
    assert best["Omsättningsklass"] == "20 000 - 49 999 tkr" and best["Län"] == "Stockholm"

    # No digital annual report: shown as such, no figures guessed, profile score only.
    for row in rows[1:]:
        assert row["Bokslut"] == "Saknas digitalt"
        assert row["Nettoomsättning TSEK"] is None and row["Nyckeltal"] is None  # not 0
        assert row["Rang"] is not None  # looked up, so ranked
    assert "en klass från målet" in rows[1]["Skäl"]  # turnover class 7 next to 6
    assert "annat län" in rows[2]["Skäl"]

    criteria = {r[0].value: r[1].value for r in wb["Kriterier"].iter_rows(min_row=2)}
    assert criteria["Omsättningsklass"] == "6 20 000 - 49 999 tkr"
    assert criteria["Län"] == "01 Stockholm"

    # Missing keys give a readable error.
    def no_key(settings: Any) -> ScbClient:
        raise ScbError("SCB_API_KEY saknas i .env.")

    monkeypatch.setattr(cli, "make_scb_client", no_key)
    failed = CliRunner().invoke(app, ["screen", "--sni", "62100", "--out", str(tmp_path)])
    assert failed.exit_code == 1 and "SCB_API_KEY" in failed.output


def test_financial_score_guards_stale_loss_making_and_odd_years() -> None:
    today = dt.date(2026, 10, 17)
    fresh = financial_score(_report("33000000", "29000000", "3600000"), today)
    assert fresh.points == 40 and fresh.status == "Årsredovisning 2025-12-31"

    # Year ended more than 18 months ago: flagged, and no growth points.
    old = _report("33000000", "29000000", "3600000").model_copy(
        update={"period_start": dt.date(2024, 1, 1), "period_end": dt.date(2024, 12, 31)}
    )
    stale = financial_score(old, today)
    assert stale.status == "Inaktuell årsredovisning 2024-12-31"
    assert stale.points == 20  # margin only
    assert any("inaktuell" in r for r in stale.reasons)

    # Fast growth at a loss earns nothing.
    loss = financial_score(_report("33000000", "29000000", "-500000"), today)
    assert loss.points == 0
    assert any("negativt" in r for r in loss.reasons)

    # A prior year revenue of 0 gives no growth figure rather than a division error.
    start_up = financial_score(_report("5000000", "0", "600000"), today)
    assert start_up.growth is None and start_up.points == 20
    assert any("0 eller negativ" in r for r in start_up.reasons)

    # Notes from the parser (for example a non twelve month year) reach the reasons.
    noted = _report("33000000", None, "3600000").model_copy(
        update={"notes": ["Föregående räkenskapsår är 549 dagar och jämförs inte"]}
    )
    assert (
        "Föregående räkenskapsår är 549 dagar och jämförs inte"
        in financial_score(noted, today).reasons
    )


class _Registry:
    """Bolagsverket stand-in whose answer depends on the organisation number."""

    def __init__(self, outcomes: dict[str, Exception | None]) -> None:
        self.outcomes = outcomes
        self.asked: list[str] = []

    def latest_report(self, org_nr: str) -> AnnualReport | None:
        self.asked.append(org_nr)
        outcome = self.outcomes[org_nr]
        if outcome is not None:
            raise outcome
        return None


def test_run_includes_ties_and_keeps_going_after_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(screening, "WORKERS", 1)  # deterministic order for the stop rule
    numbers = (1, 10, 2, 8)
    units = [scb_unit(n) for n in numbers]  # identical profiles: 60 points each
    turnover = {"6": [f"1655990{n:05d}" for n in numbers]}

    # Top 1 with four equal profiles: all four are looked up, with a note why.
    registry = _Registry({org(n): None for n in numbers})
    stages: list[str] = []
    counts: list[tuple[int, int]] = []
    tied = run_screening(
        CRITERIA,
        fake_scb(units, turnover),
        cast(Any, registry),
        top=1,
        today=dt.date(2026, 10, 17),
        progress=stages.append,
        on_report=lambda done, total: counts.append((done, total)),
    )
    assert stages == list(SCREENING_STAGES)
    assert counts[-1] == (4, 4) and len(counts) == 4
    assert tied.reports_fetched == 4 and sorted(registry.asked) == sorted(org(n) for n in numbers)
    assert any("delar profilpoängen" in n for n in tied.notes)

    # An unreadable report, an unexpected error and an outage each get their own status;
    # after the outage no more lookups are made.
    registry = _Registry(
        {
            org(1): ReportNotReadable("Årsredovisningen kunde inte läsas: trasig"),
            org(10): RuntimeError("oväntat"),
            org(2): BolagsverketUnavailable("Bolagsverket svarar inte just nu (status 503)."),
            org(8): None,
        }
    )
    result = run_screening(CRITERIA, fake_scb(units, turnover), cast(Any, registry), top=25)
    status = {c.org_nr: c.financial.status for c in result.candidates if c.financial}
    assert status == {
        org(1): "Kunde inte läsas",
        org(10): "Kunde inte läsas",
        org(2): "Kunde inte hämtas",
        org(8): "Kunde inte hämtas",
    }
    assert org(8) not in registry.asked
    assert any("Bolagsverket svarade inte" in n for n in result.notes)

    # Without Bolagsverket nothing is looked up and nothing is ranked.
    profile_only = run_screening(CRITERIA, fake_scb(units, turnover), None, top=25)
    assert profile_only.reports_fetched == 0 and not profile_only.notes
    assert {c.financial.status for c in profile_only.candidates if c.financial} == {"Ej hämtad"}

"""Buy & Build screening: filters and fit score. Pure, see docs/DECISIONS.md for weights.

Hard filters: active (ftgStat 1), aktiebolag (jurform 49), primary SNI among the targets,
and turnover and employee class at most one class from a target. The profile score (0-60)
rewards exact matches; the financial score (0-40) comes from the latest digital annual
report. Every point comes with a Swedish reason.
"""

import datetime as dt
from decimal import Decimal

from diligens.models import (
    AnnualReport,
    Candidate,
    Figure,
    FinancialScore,
    ScbUnit,
    ScreeningCriteria,
)

ACTIVE = "1"
AKTIEBOLAG = "49"
MISSING_EMPLOYEES = "0"

GROWTH_FULL, GROWTH_HALF = Decimal("0.10"), Decimal("0")
MARGIN_FULL, MARGIN_HALF = Decimal("0.10"), Decimal("0.05")
STALE_MONTHS = 18


def _distance(value: str, targets: list[str]) -> int | None:
    """Classes between `value` and the nearest target; None if not comparable."""
    if not value.isdigit():
        return None
    return min(abs(int(value) - int(t)) for t in targets)


def _class_points(
    label: str, value: str, targets: list[str], full: int, near: int
) -> tuple[int, str] | None:
    """Points and reason for a size class, or None when the unit is out of range."""
    if not targets:
        return full, f"Inget krav på {label}"
    distance = _distance(value, targets)
    if distance is None:
        return None
    if distance == 0:
        return full, f"{label.capitalize()} {value} inom målet"
    if distance == 1:
        return near, f"{label.capitalize()} {value} en klass från målet"
    return None


def profile_score(
    unit: ScbUnit, turnover_class: str, criteria: ScreeningCriteria
) -> tuple[int, list[str]] | tuple[None, list[str]]:
    """Profile points (0-60) and reasons, or None when a hard filter excludes the unit."""
    if unit.status != ACTIVE:
        return None, ["Inte verksamt"]
    if unit.legal_form != AKTIEBOLAG:
        return None, ["Inte aktiebolag"]
    if unit.primary_sni not in criteria.sni:
        return None, ["Annan primär näringsgren"]
    points, reasons = 15, [f"Primär näringsgren {unit.primary_sni}"]

    turnover = _class_points("omsättningsklass", turnover_class, criteria.turnover_classes, 15, 7)
    if turnover is None:
        return None, ["Omsättningsklass utanför målet"]
    if unit.employee_class == MISSING_EMPLOYEES and criteria.employee_classes:
        employees: tuple[int, str] | None = (0, "Antal anställda saknas hos SCB")
    else:
        employees = _class_points(
            "anställdaklass", unit.employee_class, criteria.employee_classes, 10, 5
        )
    if employees is None:
        return None, ["Anställdaklass utanför målet"]
    for part in (turnover, employees):
        points += part[0]
        reasons.append(part[1])

    if not criteria.counties:
        points += 10
        reasons.append("Inget krav på län")
    elif unit.county in criteria.counties:
        points += 10
        reasons.append(f"Säte i län {unit.county}")
    else:
        reasons.append(f"Säte i annat län ({unit.county})")
    points += 10
    reasons.append("Verksamt aktiebolag")
    return points, reasons


def select_candidates(
    units: list[ScbUnit], turnover: dict[str, str], criteria: ScreeningCriteria
) -> list[Candidate]:
    """Units that pass the filters, best profile first. `turnover` maps peOrgNr to omsKl."""
    candidates = []
    for unit in units:
        turnover_class = turnover.get(unit.pe_org_nr, "")
        points, reasons = profile_score(unit, turnover_class, criteria)
        if points is not None:
            candidates.append(
                Candidate(
                    unit=unit,
                    turnover_class=turnover_class,
                    profile_points=points,
                    profile_reasons=reasons,
                )
            )
    return sorted(candidates, key=lambda c: (-c.profile_points, c.unit.name))


def _ratio(numerator: Figure, denominator: Figure, ref: str) -> Figure | None:
    if numerator.value is None or not denominator.value:
        return None
    return Figure(value=numerator.value / denominator.value, source_ref=ref)


def _percent(value: Decimal) -> str:
    return f"{value * 100:.1f} %".replace(".", ",")


def financial_score(report: AnnualReport | None, today: dt.date | None = None) -> FinancialScore:
    """Growth and EBIT margin points (0-40) from the latest digital annual report.

    Growth needs a positive prior year revenue of a twelve month year and a non negative
    EBIT (fast growth at a loss earns nothing). A report whose year ended more than
    STALE_MONTHS before `today` is marked as old and earns no growth points.
    """
    if report is None:
        return FinancialScore(
            points=None,
            reasons=["Ingen digital årsredovisning, nyckeltal kan inte bedömas"],
            status="Saknas digitalt",
        )
    figures = report.figures
    revenue = figures.get("nettoomsattning")
    prior = figures.get("nettoomsattning_fg")
    ebit = figures.get("rorelseresultat")
    reasons: list[str] = list(report.notes)
    points = 0
    stale = today is not None and (today - report.period_end).days > STALE_MONTHS * 30.5
    status = f"Årsredovisning {report.period_end.isoformat()}"
    if stale:
        status = f"Inaktuell årsredovisning {report.period_end.isoformat()}"
        reasons.append(f"Årsredovisningen är äldre än {STALE_MONTHS} månader")

    growth = None
    if revenue and prior and revenue.value is not None and prior.value is not None:
        if prior.value > 0:
            growth = Figure(
                value=revenue.value / prior.value - 1,
                source_ref=f"{revenue.source_ref}; {prior.source_ref}",
            )
        else:
            reasons.append(
                "Tillväxt kan inte beräknas, föregående års omsättning är 0 eller negativ"
            )
    elif revenue and not prior:
        reasons.append("Tillväxt saknas, föregående år finns inte eller är inte tolv månader")
    margin = None
    if revenue and ebit:
        margin = _ratio(ebit, revenue, f"{ebit.source_ref}; {revenue.source_ref}")

    if growth is not None and growth.value is not None:
        if stale:
            reasons.append(f"Tillväxt {_percent(growth.value)} ger 0 poäng, inaktuell siffra")
        elif ebit is not None and ebit.value is not None and ebit.value < 0:
            reasons.append(
                f"Tillväxt {_percent(growth.value)} ger 0 poäng eftersom rörelseresultatet är "
                "negativt"
            )
        else:
            gain = 20 if growth.value >= GROWTH_FULL else 10 if growth.value >= GROWTH_HALF else 0
            points += gain
            reasons.append(f"Tillväxt {_percent(growth.value)} ger {gain} poäng")

    if margin is None or margin.value is None:
        reasons.append("Rörelsemarginal saknas i årsredovisningen")
    else:
        gain = 20 if margin.value >= MARGIN_FULL else 10 if margin.value >= MARGIN_HALF else 0
        points += gain
        reasons.append(f"Rörelsemarginal {_percent(margin.value)} ger {gain} poäng")

    return FinancialScore(
        points=points,
        reasons=reasons,
        status=status,
        period_end=report.period_end,
        revenue=revenue,
        revenue_prior=prior,
        ebit=ebit,
        growth=growth,
        ebit_margin=margin,
    )

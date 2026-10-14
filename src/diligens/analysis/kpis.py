"""KPIs, working capital and net debt, per docs/DOMAIN.md sections 4 and 5.

KPI values follow the DOMAIN.md definitions (revenue, costs and EBITDA positive). Net debt
items are positive when they increase net debt.
"""

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from diligens.models import (
    Figure,
    FiscalYear,
    KPIs,
    KpiSeries,
    NetDebtItem,
    NetDebtKind,
    Statements,
)

ZERO = Decimal("0")
CENT = Decimal("0.01")
CORPORATE_TAX_RATE = Decimal("0.206")

# (id, Swedish label, income statement line, sign) so that the value matches DOMAIN.md.
_LINE_KPIS: tuple[tuple[str, str, str, int], ...] = (
    ("nettoomsattning", "Nettoomsättning", "Nettoomsättning", -1),
    ("ovriga_rorelseintakter", "Övriga rörelseintäkter", "Övriga rörelseintäkter", -1),
    (
        "ravaror",
        "Råvaror och underentreprenader",
        "Råvaror, handelsvaror och underentreprenader",
        1,
    ),
    ("ovriga_externa_kostnader", "Övriga externa kostnader", "Övriga externa kostnader", 1),
    ("personalkostnader", "Personalkostnader", "Personalkostnader", 1),
    ("ovriga_rorelsekostnader", "Övriga rörelsekostnader", "Övriga rörelsekostnader", 1),
    ("avskrivningar", "Av och nedskrivningar", "Av och nedskrivningar", 1),
)

# Working capital (DOMAIN.md section 5): account ranges included, and accounts within them
# that are financing rather than operations.
_NWC_RANGES = ((1400, 1799), (2400, 2499), (2600, 2999))
_NWC_EXCLUDED_RANGES = (
    (2410, 2410),
    (2417, 2417),
    (2840, 2840),
    (2860, 2869),
    (2893, 2893),
    (2898, 2898),
)

# Net debt items: (kind, Swedish label, account ranges).
_NET_DEBT: tuple[tuple[NetDebtKind, str, tuple[tuple[int, int], ...]], ...] = (
    (
        "rantebarande",
        "Räntebärande skulder",
        ((2330, 2359), (2410, 2410), (2417, 2417), (2840, 2840)),
    ),
    ("skatt", "Skatteskulder", ((2500, 2599),)),
    ("kassa", "Kassa och bank", ((1900, 1999),)),
    ("placeringar", "Kortfristiga placeringar", ((1800, 1899),)),
)

# Debt like items left out of both NWC and net debt, for the advisor to assess.
_DEBT_LIKE: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
    ("Avsättningar", ((2200, 2299),)),
    ("Obligations- och konvertibla lån", ((2300, 2329),)),
    ("Långfristiga skulder till koncern och närstående", ((2360, 2399),)),
    ("Kortfristiga skulder till koncernföretag", ((2860, 2869),)),
    ("Skulder till närstående personer", ((2893, 2893),)),
    ("Outtagen vinstutdelning", ((2898, 2898),)),
)


def _in(account: str, ranges: tuple[tuple[int, int], ...]) -> bool:
    number = int(account)
    return any(first <= number <= last for first, last in ranges)


def _ranges_text(ranges: tuple[tuple[int, int], ...]) -> str:
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in ranges)


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _amount_series(statements: Statements) -> dict[str, KpiSeries]:
    lines = {line.line: line for line in statements.income}
    months = statements.months
    series: dict[str, KpiSeries] = {}
    for key, label, line, sign in _LINE_KPIS:
        source = lines[line]
        series[key] = KpiSeries(
            name=label,
            values={m: sign * source.values[m] for m in months},
            source_ref=f"RR {label}: {source.source_ref}",
        )

    def combine(key: str, label: str, ref: str, plus: list[str], minus: list[str]) -> None:
        values: dict[str, Decimal | None] = {}
        for m in months:
            total = ZERO
            for k in plus:
                total += series[k].values[m] or ZERO
            for k in minus:
                total -= series[k].values[m] or ZERO
            values[m] = total
        series[key] = KpiSeries(name=label, values=values, source_ref=ref)

    combine(
        "bruttoresultat",
        "Bruttoresultat",
        "Nettoomsättning minus konton 4000–4999",
        ["nettoomsattning"],
        ["ravaror"],
    )
    combine(
        "ebitda",
        "EBITDA",
        "Minus summa konton 3000–7699 och 7900–7999",
        ["nettoomsattning", "ovriga_rorelseintakter"],
        ["ravaror", "ovriga_externa_kostnader", "personalkostnader", "ovriga_rorelsekostnader"],
    )
    combine("ebit", "EBIT", "EBITDA minus konton 7700–7899", ["ebitda"], ["avskrivningar"])
    return series


def _with_margins(series: dict[str, KpiSeries], months: list[str]) -> dict[str, KpiSeries]:
    revenue = series["nettoomsattning"].values
    for key, label, base in (
        ("bruttomarginal", "Bruttomarginal", "bruttoresultat"),
        ("ebitda_marginal", "EBITDA marginal", "ebitda"),
    ):
        series[key] = KpiSeries(
            name=label,
            values={m: _ratio(series[base].values[m], revenue[m]) for m in months},
            source_ref=f"{series[base].name} / Nettoomsättning",
        )
    return series


def _ltm(monthly: dict[str, KpiSeries], months: list[str]) -> dict[str, KpiSeries]:
    ltm: dict[str, KpiSeries] = {}
    for key, s in monthly.items():
        values: dict[str, Decimal | None] = {}
        for i, m in enumerate(months):
            window = [s.values[x] for x in months[i - 11 : i + 1]] if i >= 11 else []
            values[m] = sum((v or ZERO for v in window), ZERO) if window else None
        ltm[key] = KpiSeries(
            name=f"{s.name} LTM", values=values, source_ref=f"Summa senaste 12 mån: {s.name}"
        )
    ltm = _with_margins(ltm, months)
    revenue = ltm["nettoomsattning"].values
    ltm["tillvaxt"] = KpiSeries(
        name="Tillväxt LTM",
        values={
            m: _growth(revenue[m], revenue[months[i - 12]]) if i >= 23 else None
            for i, m in enumerate(months)
        },
        source_ref="Nettoomsättning LTM jämfört med LTM 12 månader tidigare, minus 1",
    )
    return ltm


def _growth(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    ratio = _ratio(current, previous)
    return None if ratio is None else ratio - 1


def _balance_sum(
    statements: Statements, month: str, ranges: tuple[tuple[int, int], ...]
) -> tuple[list[str], Decimal]:
    accounts = [a for a in statements.account_balances if _in(a, ranges)]
    return accounts, sum((statements.account_balances[a][month] for a in accounts), ZERO)


def _nwc(statements: Statements) -> tuple[KpiSeries, Figure]:
    accounts = [
        a
        for a in statements.account_balances
        if _in(a, _NWC_RANGES) and not _in(a, _NWC_EXCLUDED_RANGES)
    ]
    values: dict[str, Decimal | None] = {
        m: sum((statements.account_balances[a][m] for a in accounts), ZERO)
        for m in statements.months
    }
    ref = (
        f"Månadens slut, konton {_ranges_text(_NWC_RANGES)} "
        f"utom {_ranges_text(_NWC_EXCLUDED_RANGES)}"
    )
    last_12 = [values[m] or ZERO for m in statements.months[-12:]]
    normal = Figure(
        value=sum(last_12, ZERO) / len(last_12) if len(last_12) == 12 else None,
        source_ref=f"Genomsnitt av rörelsekapital vid de 12 senaste månadssluten ({ref})",
    )
    return KpiSeries(name="Rörelsekapital", values=values, source_ref=ref), normal


def _estimated_tax(statements: Statements, month: str, tax_rate: Decimal) -> NetDebtItem | None:
    """Tax on this year's profit so far when the year is still open (no closing to 8999)."""
    year = next(fy for fy in statements.fiscal_years if _contains(fy, month))
    year_months = [m for m in statements.months if _contains(year, m)]
    full_year = sum(
        (
            statements.account_movements[a][m]
            for a in statements.account_movements
            for m in year_months
        ),
        ZERO,
    )
    if full_year == 0:
        return None  # closed: result booked to equity, tax already on 25xx
    so_far = [m for m in year_months if m <= month]
    pre_tax = -sum(
        (
            statements.account_movements[a][m]
            for a in statements.account_movements
            if int(a) < 8900
            for m in so_far
        ),
        ZERO,
    )
    booked = sum(
        (
            statements.account_movements[a][m]
            for a in statements.account_movements
            if 8900 <= int(a) <= 8989
            for m in so_far
        ),
        ZERO,
    )
    amount = max(ZERO, (pre_tax * tax_rate).quantize(CENT, rounding=ROUND_HALF_UP) - booked)
    return NetDebtItem(
        label="Beräknad skatt innevarande år (ej bokförd)",
        kind="beraknad_skatt",
        accounts=[],
        amount=amount,
        source_ref=(
            f"{tax_rate * 100:.1f} % av resultat före skatt {so_far[0]} till {month} "
            "(konton 3000–8899), minus bokförd skatt 8900–8989. Antagande, året är inte bokslutat."
        ),
    )


def _contains(year: FiscalYear, month: str) -> bool:
    return year.start <= dt.date(int(month[:4]), int(month[5:]), 1) <= year.end


def compute_kpis(statements: Statements, tax_rate: Decimal = CORPORATE_TAX_RATE) -> KPIs:
    months = statements.months
    monthly = _with_margins(_amount_series(statements), months)
    ltm = _ltm(
        {k: v for k, v in monthly.items() if k not in ("bruttomarginal", "ebitda_marginal")}, months
    )
    nwc, nwc_normal = _nwc(statements)

    month = months[-1]
    items: list[NetDebtItem] = []
    for kind, label, ranges in _NET_DEBT:
        accounts, raw = _balance_sum(statements, month, ranges)
        items.append(
            NetDebtItem(
                label=label,
                kind=kind,
                accounts=accounts,
                amount=-raw,
                source_ref=f"SIE saldo {month}, konton {_ranges_text(ranges)}",
            )
        )
    estimated = _estimated_tax(statements, month, tax_rate)
    if estimated is not None:
        items.append(estimated)

    debt_like: list[NetDebtItem] = []
    for label, ranges in _DEBT_LIKE:
        accounts, raw = _balance_sum(statements, month, ranges)
        if accounts and raw != 0:
            debt_like.append(
                NetDebtItem(
                    label=label,
                    kind="att_bedoma",
                    accounts=accounts,
                    amount=-raw,
                    source_ref=f"SIE saldo {month}, konton {_ranges_text(ranges)}",
                )
            )

    return KPIs(
        months=months,
        monthly=monthly,
        ltm=ltm,
        nwc=nwc,
        nwc_normal=nwc_normal,
        net_debt_month=month,
        net_debt_items=items,
        net_debt=sum((i.amount for i in items), ZERO),
        debt_like_items=debt_like,
    )

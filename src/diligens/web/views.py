"""What the app shows, built from the computed results. Pure: no IO, no new figures.

Subtotals follow the Excel layout (export/excel.py RR_LAYOUT) and are sums of computed lines;
tests check them against the KPIs, so the screen, the workbook and the analysis agree.
"""

import html
import re
from decimal import Decimal

from pydantic import BaseModel

from diligens.analysis.kpis import nwc_lines
from diligens.analysis.mapping import BAS_LINES
from diligens.export.excel import (
    CATEGORY_LABELS,
    NWC_LABELS,
    RR_LAYOUT,
    STATUS_ORDER,
    flag_statuses,
)
from diligens.formatting import format_tsek
from diligens.models import AnalysisResult, Flag, Ledger, ScreeningResult
from diligens.screening import with_ranks

ZERO = Decimal(0)
MISSING = "–"


# ------------------------------------------------------------------ formats


def tsek(value: Decimal | None) -> str:
    return MISSING if value is None else format_tsek(value)


def sek(value: Decimal | None) -> str:
    """Whole kronor with öre, as booked: '1 234,50'."""
    return "" if value is None else f"{value:,.2f}".replace(",", " ").replace(".", ",")


def integer(value: Decimal | int) -> str:
    """A whole number with space as thousands separator: '31 623'."""
    return f"{Decimal(value).normalize():,f}".replace(",", " ")


def confidence(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def percent(value: Decimal | float | None) -> str:
    if value is None:
        return MISSING
    return f"{Decimal(str(value)) * 100:.1f} %".replace(".", ",")


def month_label(month: str) -> str:
    names = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
    year, number = month.split("-")
    return f"{names[int(number) - 1]} {year[2:]}"


# --------------------------------------------------------------- statements


class Row(BaseModel):
    label: str
    values: list[Decimal | None]
    period: Decimal | None = None
    total: bool = False
    source: str = ""


def income_rows(result: AnalysisResult) -> list[Row]:
    """RR per month in presentation signs (income positive, costs negative), as in Excel."""
    statements, bridge = result.statements, result.bridge
    months = statements.months
    in_period = set(months[months.index(bridge.period[0]) : months.index(bridge.period[1]) + 1])
    lines = {line.line: line for line in statements.income}
    tax_accounts = [a for a in statements.account_movements if 8900 <= int(a) <= 8989]
    values: dict[str, dict[str, Decimal]] = {}
    rows = []
    for label, parts in RR_LAYOUT:
        if parts is not None:
            values[label] = {m: sum((values[p][m] for p in parts), ZERO) for m in months}
            source = "Summa: " + " + ".join(parts)
        elif label == "Skatt":
            values[label] = {
                m: -sum((statements.account_movements[a][m] for a in tax_accounts), ZERO)
                for m in months
            }
            source = "SIE #TRANS per verifikationsdatum, konton 8900 till 8989"
        else:
            values[label] = {m: -lines[label].values[m] for m in months}
            source = lines[label].source_ref
        row_values = values[label]
        rows.append(
            Row(
                label=label,
                values=[row_values[m] for m in months],
                period=sum((row_values[m] for m in in_period), ZERO),
                total=parts is not None,
                source=source,
            )
        )
    return rows


def balance_rows(result: AnalysisResult) -> list[Row]:
    """BR per month end: assets positive, equity and liabilities positive, and the check."""
    months = result.statements.months
    asset_lines = {line for first, _, line, kind in BAS_LINES if kind == "BR" and first < 2000}
    lines = result.statements.balance

    def line_rows(assets: bool) -> list[Row]:
        sign = 1 if assets else -1
        return [
            Row(
                label=sl.line,
                values=[sign * sl.values[m] for m in months],
                source=sl.source_ref,
            )
            for sl in lines
            if (sl.line in asset_lines) == assets
        ]

    def total(label: str, parts: list[Row]) -> Row:
        sums = [sum((p.values[i] or ZERO for p in parts), ZERO) for i in range(len(months))]
        return Row(label=label, values=list(sums), total=True, source="Summa av raderna ovan")

    assets = line_rows(True)
    claims = line_rows(False)
    asset_total = total("Summa tillgångar", assets)
    claim_total = total("Summa eget kapital och skulder", claims)
    difference = Row(
        label="Differens",
        values=[
            (a or ZERO) - (c or ZERO)
            for a, c in zip(asset_total.values, claim_total.values, strict=True)
        ],
        total=True,
        source="Tillgångar minus eget kapital och skulder, ska vara 0",
    )
    return [*assets, asset_total, *claims, claim_total, difference]


def nwc_rows(result: AnalysisResult) -> list[Row]:
    statements = result.statements
    months = statements.months
    rows = []
    for line, members in nwc_lines(statements, result.mapping):
        rows.append(
            Row(
                label=NWC_LABELS.get(line, line),
                values=[
                    sum((statements.account_balances[a][m] for a in members), ZERO) for m in months
                ],
                source=f"SIE #IB plus #TRANS vid månadens slut, konton {', '.join(members)}",
            )
        )
    rows.append(
        Row(
            label="Rörelsekapital",
            values=[result.kpis.nwc.values.get(m) for m in months],
            total=True,
            source=result.kpis.nwc.source_ref,
        )
    )
    return rows


# -------------------------------------------------------------------- flags


class FlagRow(BaseModel):
    flag: Flag
    status: str
    category: str
    reasons: str


def flag_rows(result: AnalysisResult) -> list[FlagRow]:
    """Flags with their bridge status, in the Excel order: status, then largest first."""
    statuses = flag_statuses(result)
    reasons = {h.voucher_ref: "; ".join(h.reasons) for h in result.prefilter_hits}
    rows = [
        FlagRow(
            flag=f,
            status=statuses.get(f.voucher_ref, "Normal"),
            category=CATEGORY_LABELS[f.category],
            reasons=reasons.get(f.voucher_ref, ""),
        )
        for f in result.flags
    ]
    return sorted(rows, key=lambda r: (STATUS_ORDER.index(r.status), -abs(r.flag.amount)))


def status_counts(rows: list[FlagRow]) -> list[tuple[str, int]]:
    return [(s, sum(1 for r in rows if r.status == s)) for s in STATUS_ORDER]


# -------------------------------------------------------------------- chart


class Bar(BaseModel):
    month: str
    label: str
    value: Decimal
    x: float
    y: float
    height: float
    secondary_y: float | None = None


class Chart(BaseModel):
    bars: list[Bar]
    width: float
    height: float
    bar_width: float
    zero_y: float
    line: str = ""
    ticks: list[tuple[float, str]] = []


def monthly_chart(result: AnalysisResult, width: float = 1060, height: float = 210) -> Chart:
    """Revenue bars with an EBITDA line per month; geometry only, values from the KPIs."""
    months = result.kpis.months
    revenue = [result.kpis.monthly["nettoomsattning"].values.get(m) or ZERO for m in months]
    ebitda = [result.kpis.monthly["ebitda"].values.get(m) or ZERO for m in months]
    top = max([*revenue, *ebitda, Decimal(1)])
    bottom = min([*ebitda, ZERO])
    span = float(top - bottom) or 1.0
    plot = height - 16

    def y(value: Decimal) -> float:
        return round(plot - float(value - bottom) / span * plot, 2)

    step = width / max(len(months), 1)
    bar_width = round(step * 0.56, 2)
    zero_y = y(ZERO)
    bars = []
    points = []
    for i, month in enumerate(months):
        x = round(i * step + (step - bar_width) / 2, 2)
        top_y = y(revenue[i])
        bars.append(
            Bar(
                month=month,
                label=month_label(month),
                value=revenue[i],
                x=x,
                y=min(top_y, zero_y),
                height=round(abs(zero_y - top_y), 2),
                secondary_y=y(ebitda[i]),
            )
        )
        points.append(f"{round(x + bar_width / 2, 2)},{y(ebitda[i])}")
    ticks = [(y(top), tsek(top)), (zero_y, "0")]
    return Chart(
        bars=bars,
        width=width,
        height=height,
        bar_width=bar_width,
        zero_y=zero_y,
        line=" ".join(points),
        ticks=ticks,
    )


# --------------------------------------------------------------------- memo

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def memo_html(markdown: str) -> str:
    """The memo's small markdown subset (## headings, - bullets, paragraphs) as safe HTML."""
    out: list[str] = []
    in_list = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        text = _BOLD.sub(r"<strong>\1</strong>", html.escape(line.lstrip("#- ").strip()))
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{text}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("#"):
            out.append(f"<h3>{text}</h3>")
        elif line:
            out.append(f"<p>{text}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


# ----------------------------------------------------------------- vouchers


class VoucherLine(BaseModel):
    account: str
    name: str
    debit: Decimal | None
    credit: Decimal | None
    text: str


class VoucherView(BaseModel):
    ref: str
    date: str
    text: str
    reg_date: str
    lines: list[VoucherLine]
    debit_total: Decimal
    credit_total: Decimal


def voucher_view(ledger: Ledger, ref: str) -> VoucherView | None:
    """One voucher as booked: debit and credit per line, straight from the SIE file."""
    voucher = next((v for v in ledger.vouchers if v.ref == ref), None)
    if voucher is None:
        return None
    lines = []
    for t in voucher.transactions:
        account = ledger.accounts.get(t.account)
        lines.append(
            VoucherLine(
                account=t.account,
                name=account.name if account else "",
                debit=t.amount if t.amount > 0 else None,
                credit=-t.amount if t.amount < 0 else None,
                text=t.text or "",
            )
        )
    return VoucherView(
        ref=voucher.ref,
        date=voucher.date.isoformat(),
        text=voucher.text,
        reg_date=voucher.reg_date.isoformat() if voucher.reg_date else "",
        lines=lines,
        debit_total=sum((line.debit or ZERO for line in lines), ZERO),
        credit_total=sum((line.credit or ZERO for line in lines), ZERO),
    )


# ---------------------------------------------------------------- screening


class CandidateRow(BaseModel):
    rank: int | None
    org_nr: str
    name: str
    town: str
    county: str
    turnover_class: str
    employee_class: str
    total: int
    profile: int
    financial: int | None
    status: str
    revenue: Decimal | None
    growth: Decimal | None
    margin: Decimal | None
    looked_up: bool


def candidate_rows(result: ScreeningResult) -> list[CandidateRow]:
    labels = result.labels
    rows = []
    for rank, c in with_ranks(result.candidates):
        f = c.financial
        rows.append(
            CandidateRow(
                rank=rank,
                org_nr=c.org_nr,
                name=c.unit.name,
                town=c.unit.post_town.title(),
                county=labels.get("lankoder", {}).get(c.unit.county, c.unit.county),
                turnover_class=labels.get("omsklkoder", {}).get(
                    c.turnover_class, c.turnover_class or "okänd"
                ),
                employee_class=labels.get("anstklkoder", {}).get(
                    c.unit.employee_class, c.unit.employee_class
                ),
                total=c.total,
                profile=c.profile_points,
                financial=f.points if f else None,
                status=f.status if f else "Ej hämtad",
                revenue=f.revenue.value if f and f.revenue else None,
                growth=f.growth.value if f and f.growth else None,
                margin=f.ebit_margin.value if f and f.ebit_margin else None,
                looked_up=rank is not None,
            )
        )
    return rows


def org_display(org_nr: str) -> str:
    return f"{org_nr[:6]}-{org_nr[6:]}" if len(org_nr) == 10 else org_nr

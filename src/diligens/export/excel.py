"""The DD pack as an Excel workbook. Swedish labels, TSEK, live formulas for totals.

Values are written in TSEK (SEK / 1000, unrounded), so formulas compute exactly and the
number format only controls display. Signs follow DOMAIN.md section 3: revenue positive and
costs negative in the income statement; assets, equity and liabilities positive in the
balance sheet. Every sheet has a "Källa" column.
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from diligens.analysis.kpis import nwc_accounts
from diligens.analysis.mapping import BAS_LINES
from diligens.analysis.statements import COMPUTED_RESULT
from diligens.models import AnalysisResult, BridgeItem, Flag, FlagCategory

SHEETS = [
    "Sammanfattning",
    "RR månad",
    "BR månad",
    "EBITDA brygga",
    "Rörelsekapital",
    "Nettoskuld",
    "Flaggade poster",
    "DD frågor",
    "Avstämning",
    "Källor",
]

TSEK = "#,##0.0"
SEK = "#,##0.00"
PERCENT = "0.0%"
CONFIDENCE = "0.00"
THOUSAND = Decimal(1000)

CATEGORY_LABELS: dict[FlagCategory, str] = {
    "engangspost": "Engångspost",
    "agarrelaterad": "Ägarrelaterad",
    "narstaende": "Närstående",
    "periodisering": "Periodisering",
    "avvikelse": "Avvikelse",
    "normal": "Normal",
}
STATUS_ORDER = ["Justerad", "Kräver granskning", "Ej justerad, fråga", "Utanför perioden", "Normal"]

_HEADER_FONT = Font(bold=True)
_HEADER_FILL = PatternFill("solid", fgColor="DDE3EA")
_TOTAL_FONT = Font(bold=True)

# Income statement layout: BAS lines and subtotals (label, parts) in presentation order.
RR_LAYOUT: list[tuple[str, list[str] | None]] = [
    ("Nettoomsättning", None),
    ("Råvaror, handelsvaror och underentreprenader", None),
    ("Bruttoresultat", ["Nettoomsättning", "Råvaror, handelsvaror och underentreprenader"]),
    ("Övriga rörelseintäkter", None),
    ("Övriga externa kostnader", None),
    ("Personalkostnader", None),
    ("Övriga rörelsekostnader", None),
    (
        "EBITDA",
        [
            "Bruttoresultat",
            "Övriga rörelseintäkter",
            "Övriga externa kostnader",
            "Personalkostnader",
            "Övriga rörelsekostnader",
        ],
    ),
    ("Av och nedskrivningar", None),
    ("EBIT", ["EBITDA", "Av och nedskrivningar"]),
    ("Finansiella poster", None),
    ("Bokslutsdispositioner", None),
    ("Resultat före skatt", ["EBIT", "Finansiella poster", "Bokslutsdispositioner"]),
    ("Skatt och årets resultat", None),
]


def _tsek(value: Decimal | None) -> Decimal | None:
    return None if value is None else value / THOUSAND


def _col(index: int) -> str:
    return get_column_letter(index)


def _header(ws: Worksheet, labels: list[str]) -> None:
    ws.append(labels)
    for cell in ws[1]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="top", wrap_text=True)


def _bold(ws: Worksheet, row: int) -> None:
    for cell in ws[row]:
        cell.font = _TOTAL_FONT


def _format(ws: Worksheet, columns: range | list[int], rows: range, number_format: str) -> None:
    for row in rows:
        for column in columns:
            ws.cell(row=row, column=column).number_format = number_format


def _widths(ws: Worksheet, fixed: dict[int, int] | None = None) -> None:
    fixed = fixed or {}
    for index, column in enumerate(ws.iter_cols(), start=1):
        if index in fixed:
            width = fixed[index]
        else:
            lengths = [
                len(str(c.value))
                for c in column
                if c.value is not None and not str(c.value).startswith("=")
            ]
            width = min(max([12, *lengths]) + 2, 60)
        ws.column_dimensions[_col(index)].width = width


def _income_statement(ws: Worksheet, result: AnalysisResult) -> tuple[dict[str, int], str]:
    """Write RR månad; return label to row and the period (LTM or YTD) column letter."""
    months = result.statements.months
    bridge = result.bridge
    period_title = (
        f"{'LTM' if bridge.basis == 'LTM' else 'Hittills i år'} "
        f"{bridge.period[0]} till {bridge.period[1]}"
    )
    _header(ws, ["Rad", *months, period_title, "Källa"])
    first = months.index(bridge.period[0]) + 2
    last = months.index(bridge.period[1]) + 2
    period_col = len(months) + 2
    lines = {line.line: line for line in result.statements.income}

    rows: dict[str, int] = {}
    for label, parts in RR_LAYOUT:
        row = ws.max_row + 1
        rows[label] = row
        ws.cell(row=row, column=1, value=label)
        for i, month in enumerate(months, start=2):
            letter = _col(i)
            if parts is None:
                ws.cell(row=row, column=i, value=_tsek(-lines[label].values[month]))
            else:
                ws.cell(
                    row=row, column=i, value="=" + "+".join(f"{letter}{rows[p]}" for p in parts)
                )
        ws.cell(
            row=row,
            column=period_col,
            value=f"=SUM({_col(first)}{row}:{_col(last)}{row})",
        )
        source = lines[label].source_ref if parts is None else "Formel: " + " + ".join(parts)
        ws.cell(row=row, column=period_col + 1, value=source)
        if parts is not None:
            _bold(ws, row)
    _format(ws, range(2, period_col + 1), range(2, ws.max_row + 1), TSEK)
    ws.freeze_panes = "B2"
    _widths(ws, {1: 44})
    return rows, _col(period_col)


def _balance_sheet(ws: Worksheet, result: AnalysisResult) -> None:
    months = result.statements.months
    _header(ws, ["Rad", *months, "Källa"])
    source_col = len(months) + 2
    asset_lines = {
        line for first, _, line, statement in BAS_LINES if statement == "BR" and first < 2000
    }
    lines = result.statements.balance

    def write(label: str, values: dict[str, Decimal], sign: int, source: str) -> int:
        row = ws.max_row + 1
        ws.cell(row=row, column=1, value=label)
        for i, month in enumerate(months, start=2):
            ws.cell(row=row, column=i, value=_tsek(sign * values[month]))
        ws.cell(row=row, column=source_col, value=source)
        return row

    def total(label: str, start: int, end: int) -> int:
        row = ws.max_row + 1
        ws.cell(row=row, column=1, value=label)
        for i in range(2, source_col):
            ws.cell(row=row, column=i, value=f"=SUM({_col(i)}{start}:{_col(i)}{end})")
        ws.cell(row=row, column=source_col, value=f"Formel: summa rad {start} till {end}")
        _bold(ws, row)
        return row

    asset_rows = [
        write(sl.line, sl.values, 1, sl.source_ref) for sl in lines if sl.line in asset_lines
    ]
    assets = total("Summa tillgångar", asset_rows[0], asset_rows[-1])
    claim_rows = [
        write(sl.line, sl.values, -1, sl.source_ref) for sl in lines if sl.line not in asset_lines
    ]
    claims = total("Summa eget kapital och skulder", claim_rows[0], claim_rows[-1])
    row = ws.max_row + 1
    ws.cell(row=row, column=1, value="Differens")
    for i in range(2, source_col):
        ws.cell(row=row, column=i, value=f"={_col(i)}{assets}-{_col(i)}{claims}")
    ws.cell(
        row=row,
        column=source_col,
        value=f"Formel: tillgångar minus eget kapital och skulder, ska vara 0. "
        f"{COMPUTED_RESULT} gör att balansräkningen går ihop före bokslut.",
    )
    _bold(ws, row)
    _format(ws, range(2, source_col), range(2, ws.max_row + 1), TSEK)
    ws.freeze_panes = "B2"
    _widths(ws, {1: 44})


def _refs(refs: list[str]) -> str:
    return ", ".join(refs)


def _bridge(ws: Worksheet, result: AnalysisResult, rr_rows: dict[str, int], period_col: str) -> int:
    """Write EBITDA brygga; return the row of Justerad EBITDA."""
    bridge = result.bridge
    _header(
        ws, ["Post", "Kategori", "Konton", "Verifikat", "Belopp TSEK", "Inkludera", "Not", "Källa"]
    )
    basis = "LTM" if bridge.basis == "LTM" else "hittills i år"
    ws.append(
        [
            f"Rapporterad EBITDA ({basis}, {bridge.period[0]} till {bridge.period[1]})",
            None,
            None,
            None,
            f"='RR månad'!{period_col}{rr_rows['EBITDA']}",
            None,
            None,
            "RR månad, raden EBITDA, periodkolumnen",
        ]
    )
    items: list[BridgeItem] = [*bridge.items, *bridge.candidates]
    for item in items:
        ws.append(
            [
                item.label,
                CATEGORY_LABELS[item.category],
                ", ".join(item.accounts),
                _refs(item.voucher_refs),
                _tsek(item.amount),
                1 if item.included else 0,
                item.note or "",
                item.source_ref,
            ]
        )
    adjusted = ws.max_row + 1
    formula = f"=E2+SUMPRODUCT(E3:E{adjusted - 1},F3:F{adjusted - 1})" if items else "=E2"
    ws.append(
        [
            "Justerad EBITDA",
            None,
            None,
            None,
            formula,
            None,
            "Sätt Inkludera till 1 eller 0 för att ta med eller utesluta en post.",
            "Formel: rapporterad EBITDA plus belopp gånger inkludera",
        ]
    )
    _bold(ws, adjusted)
    _format(ws, [5], range(2, adjusted + 1), TSEK)
    ws.freeze_panes = "A2"
    _widths(ws, {1: 50, 4: 40, 7: 50, 8: 60})
    return adjusted


def _working_capital(ws: Worksheet, result: AnalysisResult) -> tuple[int, int | None]:
    months = result.statements.months
    _header(ws, ["Rad", *months, "Källa"])
    source_col = len(months) + 2
    accounts = nwc_accounts(result.statements)
    order = [line for _, _, line, statement in BAS_LINES if statement == "BR"]
    by_line: dict[str, list[str]] = {}
    for account in accounts:
        by_line.setdefault(result.mapping.accounts[account].line, []).append(account)

    rows = []
    for line in [line for line in order if line in by_line]:
        members = sorted(by_line[line])
        row = ws.max_row + 1
        rows.append(row)
        ws.cell(row=row, column=1, value=line)
        for i, month in enumerate(months, start=2):
            value = sum((result.statements.account_balances[a][month] for a in members), Decimal(0))
            ws.cell(row=row, column=i, value=_tsek(value))
        ws.cell(
            row=row,
            column=source_col,
            value=f"SIE #IB plus #TRANS vid månadens slut, konton {', '.join(members)}",
        )
    nwc = ws.max_row + 1
    ws.cell(row=nwc, column=1, value="Rörelsekapital")
    for i in range(2, source_col):
        ws.cell(row=nwc, column=i, value=f"=SUM({_col(i)}{rows[0]}:{_col(i)}{rows[-1]})")
    ws.cell(row=nwc, column=source_col, value=result.kpis.nwc.source_ref)
    _bold(ws, nwc)

    normal: int | None = None
    if len(months) >= 12:
        normal = nwc + 1
        last = len(months) + 1
        ws.cell(row=normal, column=1, value="Normalnivå, snitt 12 månader")
        ws.cell(
            row=normal,
            column=last,
            value=f"=AVERAGE({_col(last - 11)}{nwc}:{_col(last)}{nwc})",
        )
        ws.cell(row=normal, column=source_col, value=result.kpis.nwc_normal.source_ref)
        _bold(ws, normal)
    _format(ws, range(2, source_col), range(2, ws.max_row + 1), TSEK)
    ws.freeze_panes = "B2"
    _widths(ws, {1: 44})
    return nwc, normal


def _net_debt(ws: Worksheet, result: AnalysisResult) -> int:
    kpis = result.kpis
    _header(ws, ["Post", "Konton", "Belopp TSEK", "Källa"])
    for item in kpis.net_debt_items:
        ws.append([item.label, ", ".join(item.accounts), _tsek(item.amount), item.source_ref])
    total = ws.max_row + 1
    ws.append(
        [
            "Nettoskuld",
            None,
            f"=SUM(C2:C{total - 1})",
            f"Formel: summa av posterna per {kpis.net_debt_month}. Positivt belopp ökar "
            "nettoskulden, negativ nettoskuld är nettokassa.",
        ]
    )
    _bold(ws, total)
    if kpis.debt_like_items:
        ws.append([])
        ws.append(["Att bedöma, ingår inte i nettoskulden", None, None, None])
        _bold(ws, ws.max_row)
        for item in kpis.debt_like_items:
            ws.append([item.label, ", ".join(item.accounts), _tsek(item.amount), item.source_ref])
    _format(ws, [3], range(2, ws.max_row + 1), TSEK)
    ws.freeze_panes = "A2"
    _widths(ws, {1: 50, 4: 70})
    return total


def _status(flag: Flag, result: AnalysisResult) -> str:
    bridge = result.bridge
    adjusted = {r for item in bridge.items for r in item.voucher_refs}
    if flag.voucher_ref in adjusted:
        return "Justerad"
    if flag.voucher_ref in {f.voucher_ref for f in bridge.review}:
        return "Kräver granskning"
    if flag.voucher_ref in {f.voucher_ref for f in bridge.not_adjusted}:
        return "Ej justerad, fråga"
    if flag.voucher_ref in {f.voucher_ref for f in bridge.outside_period}:
        return "Utanför perioden"
    return "Normal"


def _flagged(ws: Worksheet, result: AnalysisResult) -> None:
    header = [
        "Verifikat",
        "Datum",
        "Konto",
        "Kontonamn",
        "Text",
        "Belopp TSEK",
        "EBITDA-justering TSEK",
        "Kategori",
        "Konfidens",
        "Status",
        "Motivering",
        "Förfilter",
        "Källa",
    ]
    _header(ws, header)
    reasons = {h.voucher_ref: "; ".join(h.reasons) for h in result.prefilter_hits}
    rows = sorted(
        ((flag, _status(flag, result)) for flag in result.flags),
        key=lambda fs: (STATUS_ORDER.index(fs[1]), -abs(fs[0].amount)),
    )
    for flag, status in rows:
        ws.append(
            [
                flag.voucher_ref,
                flag.voucher_date,
                flag.account,
                flag.account_name,
                flag.voucher_text,
                _tsek(-flag.amount),
                _tsek(flag.ebitda_adjustment),
                CATEGORY_LABELS[flag.category],
                flag.confidence,
                status,
                flag.rationale,
                reasons.get(flag.voucher_ref, ""),
                f"SIE verifikat {flag.voucher_ref}, konto {flag.account}",
            ]
        )
    end = ws.max_row + 1
    _format(ws, [6, 7], range(2, end), TSEK)
    _format(ws, [9], range(2, end), CONFIDENCE)
    _format(ws, [2], range(2, end), "yyyy-mm-dd")
    ws.freeze_panes = "A2"
    _widths(ws, {5: 45, 11: 60, 12: 60, 13: 30})


def _questions(ws: Worksheet, result: AnalysisResult) -> None:
    _header(ws, ["Nr", "Fråga", "Konto", "Verifikat", "Belopp TSEK", "Källa"])
    for i, q in enumerate(result.questions, start=1):
        ws.append(
            [
                i,
                q.text,
                ", ".join(q.accounts),
                _refs(q.voucher_refs),
                _tsek(abs(q.amount)) if q.amount is not None else None,
                "Flaggade poster, nettoeffekt på EBITDA-konton för verifikaten",
            ]
        )
        ws.cell(row=ws.max_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    _format(ws, [5], range(2, ws.max_row + 1), TSEK)
    ws.freeze_panes = "A2"
    _widths(ws, {1: 6, 2: 90, 4: 40, 6: 45})


def _reconciliation(ws: Worksheet, result: AnalysisResult) -> None:
    rec = result.reconciliation
    years = {fy.index: f"{fy.start} till {fy.end}" for fy in result.statements.fiscal_years}
    _header(
        ws,
        [
            "Räkenskapsår",
            "Mot",
            "Konto",
            "Beräknat SEK",
            "Enligt fil SEK",
            "Differens SEK",
            "Källa",
        ],
    )
    for r in rec.rows:
        row = ws.max_row + 1
        ws.append(
            [
                years.get(r.year_index, str(r.year_index)),
                r.against,
                r.account,
                r.computed,
                r.stated,
                f"=D{row}-E{row}",
                f"Månadsrapporter mot SIE #{r.against} {r.year_index}, konto {r.account}",
            ]
        )
    end = ws.max_row
    _format(ws, [4, 5, 6], range(2, end + 1), SEK)
    ws.append([])
    ws.append(["Status", "OK" if rec.ok else "AVVIKER", None, None, None, None, "Avstämningen"])
    _bold(ws, ws.max_row)
    ws.append(
        [
            "Omappade konton",
            ", ".join(rec.unmapped_accounts) or "inga",
            None,
            None,
            None,
            None,
            "BAS-mappning, DOMAIN.md avsnitt 2",
        ]
    )
    ws.append(
        [
            "Verifikat utanför räkenskapsåren",
            ", ".join(rec.vouchers_outside_years) or "inga",
            None,
            None,
            None,
            None,
            "SIE #VER mot #RAR",
        ]
    )
    ws.freeze_panes = "A2"
    _widths(ws, {7: 60})


def _summary(
    ws: Worksheet,
    result: AnalysisResult,
    rr_rows: dict[str, int],
    period_col: str,
    adjusted_row: int,
    nwc: tuple[int, int | None],
    net_debt_row: int,
) -> None:
    kpis, bridge = result.kpis, result.bridge
    label = "LTM" if bridge.basis == "LTM" else "hittills i år"
    months = result.statements.months
    last_col = _col(len(months) + 1)
    _header(ws, ["Uppgift", "Värde", "Källa"])
    rows: list[tuple[str, Any, str, str | None]] = [
        ("Bolag", result.company_name, "SIE #FNAMN", None),
        ("Organisationsnummer", result.org_nr, "SIE #ORGNR", None),
        ("Källfil", result.source, "SIE4-fil", None),
        ("Analyserad period", f"{months[0]} till {months[-1]}", "Verifikationsdatum", None),
        (
            "AI-stöd",
            result.model or "Av (--no-llm), flaggor kräver granskning",
            "DILIGENS_MODEL",
            None,
        ),
        (
            f"Nettoomsättning {label}",
            f"='RR månad'!{period_col}{rr_rows['Nettoomsättning']}",
            "RR månad",
            TSEK,
        ),
        (f"EBITDA {label}", f"='RR månad'!{period_col}{rr_rows['EBITDA']}", "RR månad", TSEK),
        (f"EBITDA-marginal {label}", "=B8/B7", "Formel: EBITDA delat med nettoomsättning", PERCENT),
        ("Justerad EBITDA", f"='EBITDA brygga'!E{adjusted_row}", "EBITDA brygga", TSEK),
    ]
    if len(months) >= 24:
        n = len(months) + 1
        revenue = rr_rows["Nettoomsättning"]
        rows.append(
            (
                "Tillväxt LTM",
                f"=SUM('RR månad'!{_col(n - 11)}{revenue}:{_col(n)}{revenue})"
                f"/SUM('RR månad'!{_col(n - 23)}{revenue}:{_col(n - 12)}{revenue})-1",
                "Formel: LTM mot LTM tolv månader tidigare",
                PERCENT,
            )
        )
    rows += [
        (
            f"Rörelsekapital {months[-1]}",
            f"='Rörelsekapital'!{last_col}{nwc[0]}",
            "Rörelsekapital",
            TSEK,
        ),
    ]
    if nwc[1] is not None:
        rows.append(
            (
                "Rörelsekapital normalnivå",
                f"='Rörelsekapital'!{last_col}{nwc[1]}",
                "Rörelsekapital, snitt 12 månader",
                TSEK,
            )
        )
    rows += [
        (
            f"Nettoskuld {kpis.net_debt_month}",
            f"='Nettoskuld'!C{net_debt_row}",
            "Nettoskuld, negativt värde är nettokassa",
            TSEK,
        ),
        (
            "Avstämning",
            "OK" if result.reconciliation.ok else "AVVIKER",
            "Avstämning mot #RES och #UB",
            None,
        ),
        ("Flaggade poster", len(result.flags), "Flaggade poster", None),
        ("Kräver granskning", len(bridge.review), "Flaggade poster, status", None),
        ("DD-frågor", len(result.questions), "DD frågor", None),
    ]
    for name, value, source, number_format in rows:
        ws.append([name, value, source])
        if number_format:
            ws.cell(row=ws.max_row, column=2).number_format = number_format
    ws.freeze_panes = "A2"
    _widths(ws, {1: 32, 2: 40, 3: 50})


def _sources(ws: Worksheet, result: AnalysisResult) -> None:
    _header(ws, ["Uppgift", "Värde", "Källa"])
    rows = [
        ("Källfil", result.source, "SIE4, CP437"),
        ("Bolag", f"{result.company_name} ({result.org_nr})", "SIE #FNAMN och #ORGNR"),
        ("Genererad", dt.date.today().isoformat(), "Diligens"),
        ("AI-modell", result.model or "ingen (--no-llm)", "DILIGENS_MODEL"),
        ("Belopp", "TSEK, beräknade i kronor och delade med 1 000", "Kod, inte AI"),
        (
            "Tecken",
            "RR: intäkter positiva, kostnader negativa. BR: tillgångar, eget kapital och "
            "skulder positiva.",
            "DOMAIN.md avsnitt 3",
        ),
        (
            "Konfidensgräns",
            "Flaggor under 0,7 justerar inte EBITDA och listas som Kräver granskning",
            "DECISIONS.md",
        ),
        (
            "Siffror",
            "Alla belopp och nyckeltal är beräknade i kod. AI väljer bara kategori, motivering "
            "och frågetext.",
            "Diligens",
        ),
        (
            "Spårbarhet",
            "Varje flaggad post har verifikat och konto, varje nyckeltal sin källa",
            "Kolumnen Källa",
        ),
    ]
    for row in rows:
        ws.append(list(row))
    ws.freeze_panes = "A2"
    _widths(ws, {1: 20, 2: 90, 3: 30})


def export_dd_pack(result: AnalysisResult, out_dir: Path) -> Path:
    """Write `dd_pack.xlsx` to `out_dir` and return its path."""
    wb = Workbook()
    sheets = {name: wb.create_sheet(name) for name in SHEETS}
    wb.remove(wb.worksheets[0])  # the default empty sheet

    rr_rows, period_col = _income_statement(sheets["RR månad"], result)
    _balance_sheet(sheets["BR månad"], result)
    adjusted_row = _bridge(sheets["EBITDA brygga"], result, rr_rows, period_col)
    nwc = _working_capital(sheets["Rörelsekapital"], result)
    net_debt_row = _net_debt(sheets["Nettoskuld"], result)
    _flagged(sheets["Flaggade poster"], result)
    _questions(sheets["DD frågor"], result)
    _reconciliation(sheets["Avstämning"], result)
    _summary(sheets["Sammanfattning"], result, rr_rows, period_col, adjusted_row, nwc, net_debt_row)
    _sources(sheets["Källor"], result)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "dd_pack.xlsx"
    wb.save(path)
    return path

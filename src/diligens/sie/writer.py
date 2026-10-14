"""SIE4 writer, the inverse of parser.parse_sie. Output is CP437 with CRLF line endings."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

from diligens.models import Balance, Ledger, Transaction


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _date(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def _amount(value: Decimal) -> str:
    return f"{value:.2f}"


def _balance(tag: str, b: Balance) -> str:
    return f"{tag} {b.year_index} {b.account} {_amount(b.amount)}"


def _trans(t: Transaction) -> str:
    objects = " ".join(f"{dim} {_quote(obj)}" for dim, obj in t.objects)
    parts = ["#TRANS", t.account, f"{{{objects}}}", _amount(t.amount)]
    if t.date is not None or t.text is not None:
        parts.append(_date(t.date) if t.date else '""')
    if t.text is not None:
        parts.append(_quote(t.text))
    return " ".join(parts)


def render_sie(ledger: Ledger) -> str:
    """Render a Ledger as SIE4 text with LF line endings."""
    lines = ["#FLAGGA 0"]
    if ledger.program:
        lines.append(f"#PROGRAM {_quote(ledger.program)} 1.0")
    lines.append("#FORMAT PC8")
    if ledger.generated:
        lines.append(f"#GEN {_date(ledger.generated)}")
    lines += [
        "#SIETYP 4",
        f"#FNAMN {_quote(ledger.company_name)}",
        f"#ORGNR {ledger.org_nr}",
    ]
    lines += [
        f"#RAR {fy.index} {_date(fy.start)} {_date(fy.end)}"
        for fy in sorted(ledger.fiscal_years, key=lambda fy: -fy.index)
    ]
    if ledger.chart_type:
        lines.append(f"#KPTYP {ledger.chart_type}")
    for number in sorted(ledger.accounts):
        lines.append(f"#KONTO {number} {_quote(ledger.accounts[number].name)}")
    for number in sorted(ledger.accounts):
        if ledger.accounts[number].type:
            lines.append(f"#KTYP {number} {ledger.accounts[number].type}")
    lines += [_balance("#IB", b) for b in ledger.opening_balances]
    lines += [_balance("#UB", b) for b in ledger.closing_balances]
    lines += [_balance("#RES", b) for b in ledger.results]
    lines += [
        f"#PSALDO {p.year_index} {p.period} {p.account} {{}} {_amount(p.amount)}"
        for p in ledger.period_balances
    ]
    for v in ledger.vouchers:
        header = f"#VER {v.series} {v.number} {_date(v.date)} {_quote(v.text)}"
        if v.reg_date:
            header += f" {_date(v.reg_date)}"
        lines += [header, "{", *(_trans(t) for t in v.transactions), "}"]
    return "\n".join(lines) + "\n"


def write_sie(ledger: Ledger, path: Path) -> None:
    """Write a Ledger to `path` as SIE4 (CP437, CRLF), as Swedish accounting programs do."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_sie(ledger).replace("\n", "\r\n").encode("cp437"))

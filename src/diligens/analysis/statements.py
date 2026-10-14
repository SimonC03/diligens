"""Monthly income statement and balance sheet, per docs/DOMAIN.md section 3.

Raw SIE signs throughout; presentation flips signs at export.
"""

import datetime as dt
from decimal import Decimal

from diligens.analysis.mapping import BAS_LINES
from diligens.models import FiscalYear, Ledger, Mapping, StatementLine, Statements

COMPUTED_RESULT = "Årets resultat (beräknat)"
ZERO = Decimal("0")


def month_key(date: dt.date) -> str:
    return f"{date.year}-{date.month:02d}"


def months_of(year: FiscalYear) -> list[str]:
    months: list[str] = []
    y, m = year.start.year, year.start.month
    while (y, m) <= (year.end.year, year.end.month):
        months.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def monthly_statements(ledger: Ledger, mapping: Mapping) -> Statements:
    """RR movements per calendar month (voucher date) and BR month end balances.

    Covers every fiscal year from the first to the last one with vouchers, so months stay
    contiguous, and stops at the last month with vouchers: an export taken mid year has no
    empty trailing months. Each year's BR starts from its #IB. Vouchers outside those years
    and unmapped accounts are left out; reconcile() reports them.
    """
    ordered = sorted(ledger.fiscal_years, key=lambda fy: fy.start)
    dated = [v.date for v in ledger.vouchers if any(fy.start <= v.date <= fy.end for fy in ordered)]
    if dated:
        first_date, last_date = min(dated), max(dated)
        years = [fy for fy in ordered if fy.end >= first_date and fy.start <= last_date]
        last_month = month_key(last_date)
    else:
        years, last_month = [], ""
    months_by_year = {fy.index: [m for m in months_of(fy) if m <= last_month] for fy in years}
    months = [m for fy in years for m in months_by_year[fy.index]]
    covered = set(months)

    rr = sorted(a for a, m in mapping.accounts.items() if m.statement == "RR")
    br = sorted(a for a, m in mapping.accounts.items() if m.statement == "BR")

    moved: dict[str, dict[str, Decimal]] = {}
    for voucher in ledger.vouchers:
        month = month_key(voucher.date)
        if month not in covered:
            continue
        for t in voucher.transactions:
            per_month = moved.setdefault(t.account, {})
            per_month[month] = per_month.get(month, ZERO) + t.amount

    account_movements = {a: {m: moved.get(a, {}).get(m, ZERO) for m in months} for a in rr}

    account_balances: dict[str, dict[str, Decimal]] = {a: {} for a in br}
    computed_result: dict[str, Decimal] = {}
    for fy in years:
        opening = {b.account: b.amount for b in ledger.opening_balances if b.year_index == fy.index}
        result = ZERO
        running = {a: opening.get(a, ZERO) for a in br}
        for month in months_by_year[fy.index]:
            for a in br:
                running[a] += moved.get(a, {}).get(month, ZERO)
                account_balances[a][month] = running[a]
            result += sum((account_movements[a][month] for a in rr), ZERO)
            computed_result[month] = result

    income: list[StatementLine] = []
    balance: list[StatementLine] = []
    for first, last, line, statement in BAS_LINES:
        accounts = [
            a for a in (rr if statement == "RR" else br) if mapping.accounts[a].line == line
        ]
        source = account_movements if statement == "RR" else account_balances
        values = {m: sum((source[a][m] for a in accounts), ZERO) for m in months}
        if statement == "RR":
            ref = f"SIE #TRANS per verifikationsdatum, konton {first}–{last}"
            income.append(
                StatementLine(
                    line=line, statement="RR", accounts=accounts, values=values, source_ref=ref
                )
            )
        else:
            ref = f"SIE #IB plus #TRANS till månadens slut, konton {first}–{last}"
            balance.append(
                StatementLine(
                    line=line, statement="BR", accounts=accounts, values=values, source_ref=ref
                )
            )
    balance.append(
        StatementLine(
            line=COMPUTED_RESULT,
            statement="BR",
            accounts=[],
            values=computed_result,
            source_ref="Summa resultatkonton 3000–8999 hittills under räkenskapsåret",
        )
    )

    return Statements(
        fiscal_years=years,
        months=months,
        income=income,
        balance=balance,
        account_movements=account_movements,
        account_balances=account_balances,
    )

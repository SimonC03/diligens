"""Reconcile the monthly statements against #RES and #UB in the SIE file."""

from decimal import Decimal
from typing import Literal

from diligens.analysis.mapping import map_accounts
from diligens.analysis.statements import months_of
from diligens.models import Balance, Ledger, Reconciliation, ReconciliationRow, Statements

ZERO = Decimal("0")


def _stated(balances: list[Balance], year_index: int) -> dict[str, Decimal]:
    return {b.account: b.amount for b in balances if b.year_index == year_index}


def reconcile(ledger: Ledger, statements: Statements) -> Reconciliation:
    """Per fiscal year: sum of monthly RR per account vs #RES, BR at the last month vs #UB.

    Every account with a value in either source gets a row; a missing value counts as 0.00.
    Unmapped accounts and vouchers outside every fiscal year are listed, since the
    statements leave them out.
    """
    rows: list[ReconciliationRow] = []
    for year in statements.fiscal_years:
        in_year = set(months_of(year))
        months = [m for m in statements.months if m in in_year]
        if not months:
            continue
        checks: tuple[tuple[Literal["RES", "UB"], dict[str, Decimal], dict[str, Decimal]], ...] = (
            (
                "RES",
                {
                    a: sum((v[m] for m in months), ZERO)
                    for a, v in statements.account_movements.items()
                },
                _stated(ledger.results, year.index),
            ),
            (
                "UB",
                {a: v[months[-1]] for a, v in statements.account_balances.items()},
                _stated(ledger.closing_balances, year.index),
            ),
        )
        for against, computed, stated in checks:
            accounts = {a for a, v in computed.items() if v != 0} | stated.keys()
            for account in sorted(accounts):
                rows.append(
                    ReconciliationRow(
                        year_index=year.index,
                        account=account,
                        against=against,
                        computed=computed.get(account, ZERO),
                        stated=stated.get(account, ZERO),
                    )
                )

    outside = [
        v.ref
        for v in ledger.vouchers
        if not any(fy.start <= v.date <= fy.end for fy in ledger.fiscal_years)
    ]
    return Reconciliation(
        rows=rows,
        unmapped_accounts=map_accounts(ledger).unmapped,
        vouchers_outside_years=outside,
    )

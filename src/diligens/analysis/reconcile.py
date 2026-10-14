"""Reconcile the monthly statements against #RES and #UB in the SIE file."""

from decimal import Decimal

from diligens.analysis.mapping import map_accounts
from diligens.analysis.statements import months_of
from diligens.models import Balance, Ledger, Reconciliation, ReconciliationRow, Statements

ZERO = Decimal("0")


def _stated(balances: list[Balance], year_index: int) -> dict[str, Decimal]:
    return {b.account: b.amount for b in balances if b.year_index == year_index}


def reconcile(ledger: Ledger, statements: Statements) -> Reconciliation:
    """Per fiscal year: sum of monthly RR per account vs #RES, BR at year end vs #UB.

    Every mapped account that has a value in either source gets a row; a missing value
    counts as 0.00. Unmapped accounts are listed, since the statements leave them out.
    """
    rows: list[ReconciliationRow] = []
    for year in statements.fiscal_years:
        months = months_of(year)
        checks = (
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
            for account in sorted(a for a in computed if computed[a] != 0 or a in stated):
                rows.append(
                    ReconciliationRow(
                        year_index=year.index,
                        account=account,
                        against=against,
                        computed=computed[account],
                        stated=stated.get(account, ZERO),
                    )
                )
    return Reconciliation(rows=rows, unmapped_accounts=map_accounts(ledger).unmapped)

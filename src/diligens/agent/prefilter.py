"""Deterministic prefilter: which vouchers are worth a closer look.

Only vouchers that hit at least one rule go to the LLM, which keeps cost down and makes
the selection explainable. Rules and thresholds are logged in docs/DECISIONS.md. Pure: no
IO and no LLM.
"""

import re
import statistics
from collections import defaultdict
from decimal import Decimal

from diligens.analysis.statements import month_key
from diligens.models import Ledger, PrefilterHit, Statements, Voucher

ZERO = Decimal("0")

MATERIALITY_SHARE = Decimal("0.0025")  # of annualised net revenue
MATERIALITY_FLOOR = Decimal("10000")
LARGE_MULTIPLE = 3  # times the account's median voucher amount
MIN_HISTORY = 6  # vouchers on an account before "atypical" means anything
TYPICAL_BAND = Decimal("0.25")  # within 25 % of the median counts as typical
RECURRING_MONTHS = 6
SPIKE_MULTIPLE = 2
ROUND_UNIT = 5000
ROUND_MINIMUM = 25000
YEAR_END_DAYS = 5
OWNER_ACCOUNTS = (5610, 5699)  # personbilar
OWNER_WORDS = ("ägar", "privat", "familj", "holding", "närstående")
MONTHS = (
    "januari",
    "februari",
    "mars",
    "april",
    "maj",
    "juni",
    "juli",
    "augusti",
    "september",
    "oktober",
    "november",
    "december",
)
_LATER_PERIOD = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{4})\b")


def _in_ebitda(account: str) -> bool:
    if not (len(account) == 4 and account.isdigit()):
        return False
    number = int(account)
    return 3000 <= number <= 7699 or 7900 <= number <= 7999


def _lines(voucher: Voucher) -> dict[str, Decimal]:
    """EBITDA account to net amount on this voucher."""
    lines: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in voucher.transactions:
        if _in_ebitda(t.account):
            lines[t.account] += t.amount
    return {a: v for a, v in lines.items() if v != 0}


def _normalised(text: str) -> str:
    return " ".join(re.sub(r"[\d\-./,:]+", " ", text.lower()).split())


def _materiality(statements: Statements) -> Decimal:
    revenue = -sum(
        (
            amount
            for account, per_month in statements.account_movements.items()
            if 3000 <= int(account) <= 3699
            for amount in per_month.values()
        ),
        ZERO,
    )
    months = len(statements.months) or 1
    return max(MATERIALITY_FLOOR, revenue / months * 12 * MATERIALITY_SHARE)


def prefilter(ledger: Ledger, statements: Statements) -> list[PrefilterHit]:
    """Vouchers in the analysed months that match at least one rule, in ledger order."""
    covered = set(statements.months)
    vouchers = [v for v in ledger.vouchers if month_key(v.date) in covered]
    lines = {v.ref: _lines(v) for v in vouchers}
    materiality = _materiality(statements)

    amounts: dict[str, list[Decimal]] = defaultdict(list)
    month_totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    recurring: dict[tuple[str, Decimal, str], set[str]] = defaultdict(set)
    for v in vouchers:
        for account, amount in lines[v.ref].items():
            amounts[account].append(abs(amount))
            month_totals[account][month_key(v.date)] += amount
            recurring[(account, abs(amount), _normalised(v.text))].add(month_key(v.date))
    median = {a: Decimal(statistics.median(values)) for a, values in amounts.items()}

    def typical(account: str, amount: Decimal) -> bool:
        m = median[account]
        return m > 0 and abs(abs(amount) - m) <= TYPICAL_BAND * m

    spikes: set[tuple[str, str]] = set()
    for account, totals in month_totals.items():
        for month in statements.months:
            current = abs(totals.get(month, ZERO))
            others = [abs(totals.get(m, ZERO)) for m in statements.months if m != month]
            mean_other = sum(others, ZERO) / len(others) if others else ZERO
            if current >= SPIKE_MULTIPLE * mean_other and current - mean_other >= materiality / 2:
                spikes.add((account, month))

    year_ends = [fy.end for fy in ledger.fiscal_years]
    hits: list[PrefilterHit] = []
    for v in vouchers:
        reasons: list[str] = []
        triggered: set[str] = set()

        def hit(
            reason: str, account: str, reasons: list[str] = reasons, triggered: set[str] = triggered
        ) -> None:
            if reason not in reasons:
                reasons.append(reason)
            triggered.add(account)

        text = v.text.lower()
        month = month_key(v.date)
        for account, amount in sorted(lines[v.ref].items()):
            size = abs(amount)
            number = int(account)
            few = len(amounts[account]) < MIN_HISTORY
            if size >= materiality and (few or size >= LARGE_MULTIPLE * median[account]):
                hit(f"Väsentligt belopp på konto {account}", account)
            if size >= ROUND_MINIMUM and size % ROUND_UNIT == 0:
                hit(f"Runt belopp på konto {account}", account)
            if len(recurring[(account, size, _normalised(v.text))]) >= RECURRING_MONTHS:
                hit(f"Återkommande motpart och belopp på konto {account}", account)
            if OWNER_ACCOUNTS[0] <= number <= OWNER_ACCOUNTS[1] or any(
                w in text for w in OWNER_WORDS
            ):
                hit("Ägarrelaterat konto eller text", account)
            if (account, month) in spikes and not typical(account, amount):
                hit(f"Avvikande månad på kontot {account} ({month})", account)
            if 3000 <= number <= 3999 and amount < 0:
                near_year_end = any(0 <= (end - v.date).days < YEAR_END_DAYS for end in year_ends)
                if near_year_end and size >= materiality:
                    hit("Intäkt vid periodgräns", account)
                for name, year in _LATER_PERIOD.findall(text):
                    if (int(year), MONTHS.index(name) + 1) > (v.date.year, v.date.month):
                        hit(f"Intäkt vid periodgräns, texten avser {name} {year}", account)
            if 3900 <= number <= 3999:
                hit(f"Övrig rörelseintäkt på konto {account}", account)

        if reasons:
            hits.append(
                PrefilterHit(voucher_ref=v.ref, accounts=sorted(triggered), reasons=reasons)
            )
    return hits

"""Deterministic prefilter: which vouchers are worth a closer look.

Only vouchers that hit at least one rule go to the LLM, which keeps cost down and makes
the selection explainable. Rules and thresholds are logged in docs/DECISIONS.md. Pure: no
IO and no LLM.
"""

import calendar
import datetime as dt
import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal

from diligens.analysis.bridge import ebitda_lines
from diligens.analysis.statements import month_key
from diligens.models import Ledger, PrefilterHit, Statements

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
CREDIT_NOTE_DAYS = 10
OTHER_INCOME_SHARE = Decimal("0.25")  # of materiality, keeps small FX gains out
OWNER_ACCOUNTS = (
    (5610, 5619),  # personbilar
    (5670, 5679),  # båt, flygplan, helikopter
    (6070, 6072),  # representation
    (7220, 7229),  # löner till företagsledare
    (7240, 7249),  # styrelsearvoden
)
OWNER_WORDS = ("ägar", "privat", "familj", "holding", "närstående", "förvaltning", "invest")
CORRECTION_WORDS = ("återför", "kreditering", "kreditfaktura", "rättelse", "korriger", "makuler")
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


def _period_ends(ledger: Ledger, statements: Statements) -> list[dt.date]:
    """Fiscal year ends plus the end of the last analysed month (the LTM cut-off)."""
    ends = {fy.end for fy in ledger.fiscal_years}
    if statements.months:
        year, month = (int(x) for x in statements.months[-1].split("-"))
        ends.add(dt.date(year, month, calendar.monthrange(year, month)[1]))
    return sorted(ends)


def prefilter(
    ledger: Ledger, statements: Statements, related_parties: Sequence[str] = ()
) -> list[PrefilterHit]:
    """Vouchers in the analysed months that match at least one rule, in ledger order.

    `related_parties` are names the advisor knows belong to the owner's sphere; a voucher
    whose text mentions one counts as owner related.
    """
    covered = set(statements.months)
    vouchers = [v for v in ledger.vouchers if month_key(v.date) in covered]
    lines = {v.ref: ebitda_lines(v) for v in vouchers}
    materiality = _materiality(statements)
    owner_words = (*OWNER_WORDS, *(name.lower() for name in related_parties if name.strip()))

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

    period_ends = _period_ends(ledger, statements)
    hits: list[PrefilterHit] = []
    for v in vouchers:
        reasons: list[str] = []
        triggered: set[str] = set()
        series: str | None = None

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
            key = (account, size, _normalised(v.text))
            if len(recurring[key]) >= RECURRING_MONTHS:
                hit(f"Återkommande motpart och belopp på konto {account}", account)
                series = series or f"{account}|{size}|{key[2]}"
            owner_account = any(first <= number <= last for first, last in OWNER_ACCOUNTS)
            if owner_account or any(w in text for w in owner_words):
                hit("Ägarrelaterat konto eller text", account)
            if (account, month) in spikes and not typical(account, amount):
                hit(f"Avvikande månad på kontot {account} ({month})", account)
            if 3000 <= number <= 3999 and amount < 0:
                near_end = any(0 <= (end - v.date).days < YEAR_END_DAYS for end in period_ends)
                if near_end and size >= materiality:
                    hit("Intäkt vid periodgräns", account)
                for name, year in _LATER_PERIOD.findall(text):
                    if (int(year), MONTHS.index(name) + 1) > (v.date.year, v.date.month):
                        hit(f"Intäkt vid periodgräns, texten avser {name} {year}", account)
            if 3000 <= number <= 3699 and amount > 0 and size >= materiality / 2:
                after_end = any(0 < (v.date - end).days <= CREDIT_NOTE_DAYS for end in period_ends)
                if after_end:
                    hit("Kreditering efter periodgräns", account)
            if any(w in text for w in CORRECTION_WORDS):
                hit("Återföring eller rättelse", account)
            if 3900 <= number <= 3999 and size >= materiality * OTHER_INCOME_SHARE:
                hit(f"Övrig rörelseintäkt på konto {account}", account)
            if 7970 <= number <= 7979 and size >= materiality * OTHER_INCOME_SHARE:
                hit(f"Förlust vid avyttring på konto {account}", account)

        if reasons:
            hits.append(
                PrefilterHit(
                    voucher_ref=v.ref, accounts=sorted(triggered), reasons=reasons, series=series
                )
            )
    return hits

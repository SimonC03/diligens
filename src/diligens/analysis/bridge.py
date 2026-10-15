"""Adjusted EBITDA bridge from flags, per docs/DOMAIN.md section 6 and docs/DECISIONS.md.

Adjustments are computed here from the vouchers, never taken from the LLM: the amount of a
flag is the net of its lines on EBITDA accounts, with the sign that removes the item from
EBITDA. Only flags with confidence >= 0.7 adjust, only for vouchers inside the period, and
only when the voucher has a single EBITDA account. A collective voucher (payroll batch,
daily sales journal) goes to review, since adding it back whole would overstate EBITDA.
"""

from collections import defaultdict
from decimal import Decimal

from diligens.analysis.mapping import is_ebitda_account
from diligens.analysis.statements import month_key, months_of
from diligens.models import (
    REVIEW_CONFIDENCE,
    BridgeItem,
    EbitdaBridge,
    Flag,
    FlagCategory,
    KPIs,
    Ledger,
    Voucher,
)

ZERO = Decimal("0")
COLLECTIVE_NOTE = (
    " Samlingsverifikat med flera resultatkonton, justeras inte automatiskt utan granskas."
)

# Categories that adjust EBITDA, with their Swedish label and note.
_ADJUSTING: dict[FlagCategory, tuple[str, str | None]] = {
    "engangspost": ("Engångspost", None),
    "agarrelaterad": (
        "Ägarrelaterad kostnad",
        "Hela kostnaden är återlagd; en marknadsmässig ersättning är inte uppskattad.",
    ),
    "periodisering": ("Periodisering", "Posten är flyttad ut ur perioden."),
}
_NOT_ADJUSTING: set[FlagCategory] = {"narstaende", "avvikelse"}


def ebitda_lines(voucher: Voucher) -> dict[str, Decimal]:
    """EBITDA account to net raw amount on the voucher, zero nets left out."""
    lines: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in voucher.transactions:
        if is_ebitda_account(t.account):
            lines[t.account] += t.amount
    return {a: v for a, v in lines.items() if v != 0}


def ebitda_effect(voucher: Voucher) -> Decimal:
    """Raw net of the voucher's EBITDA lines; adding it back removes the item from EBITDA."""
    return sum(ebitda_lines(voucher).values(), ZERO)


def _period(kpis: KPIs, ledger: Ledger) -> tuple[str, Decimal, list[str]]:
    month = kpis.months[-1]
    ltm = kpis.ltm["ebitda"].values.get(month)
    if ltm is not None:
        return "LTM", ltm, kpis.months[-12:]
    year = next(fy for fy in ledger.fiscal_years if month in set(months_of(fy)))
    in_year = set(months_of(year))
    period = [m for m in kpis.months if m in in_year and m <= month]
    return "YTD", kpis.ytd["ebitda"].values[month] or ZERO, period


def ebitda_bridge(kpis: KPIs, ledger: Ledger, flags: list[Flag]) -> EbitdaBridge:
    """LTM (or YTD when there is no LTM yet) EBITDA, adjusted by confident flags."""
    basis, reported, period = _period(kpis, ledger)
    in_period = set(period)

    vouchers = {v.ref: v for v in ledger.vouchers}
    groups: dict[tuple[FlagCategory, str], list[tuple[Flag, Voucher]]] = defaultdict(list)
    review: list[Flag] = []
    not_adjusted: list[Flag] = []
    outside: list[Flag] = []
    out: list[Flag] = []
    for flag in flags:
        voucher = vouchers[flag.voucher_ref]
        adjustment = ZERO
        if flag.category != "normal" and flag.confidence < REVIEW_CONFIDENCE:
            review.append(flag)
        elif flag.category in _NOT_ADJUSTING:
            not_adjusted.append(flag)
        elif flag.category in _ADJUSTING:
            if len(ebitda_lines(voucher)) > 1:
                flag = flag.model_copy(update={"rationale": flag.rationale + COLLECTIVE_NOTE})
                review.append(flag)
            elif month_key(voucher.date) not in in_period:
                outside.append(flag)
            else:
                adjustment = ebitda_effect(voucher)
                groups[(flag.category, flag.account)].append((flag, voucher))
        out.append(flag.model_copy(update={"ebitda_adjustment": adjustment}))

    items: list[BridgeItem] = []
    for (category, account), members in sorted(groups.items()):
        label, note = _ADJUSTING[category]
        name = ledger.accounts[account].name if account in ledger.accounts else account
        refs = [f.voucher_ref for f, _ in members]
        shown = ", ".join(refs[:5]) + (f" m.fl. ({len(refs)} st)" if len(refs) > 5 else "")
        items.append(
            BridgeItem(
                category=category,
                label=f"{label}: {name}",
                accounts=sorted({a for _, v in members for a in ebitda_lines(v)}),
                voucher_refs=refs,
                amount=sum((ebitda_effect(v) for _, v in members), ZERO),
                note=note,
                source_ref=(
                    f"Verifikat {shown}, EBITDA-konton på verifikaten, "
                    f"{period[0]} till {period[-1]}"
                ),
            )
        )

    return EbitdaBridge(
        basis=basis,
        month=period[-1],
        period=[period[0], period[-1]],
        reported=reported,
        items=items,
        adjusted=reported + sum((i.amount for i in items), ZERO),
        review=review,
        not_adjusted=not_adjusted,
        outside_period=outside,
        flags=out,
    )

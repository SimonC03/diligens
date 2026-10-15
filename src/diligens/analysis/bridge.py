"""Adjusted EBITDA bridge from flags, per docs/DOMAIN.md section 6 and docs/DECISIONS.md.

Adjustments are computed here from the vouchers, never taken from the LLM: the amount of a
flag is the net of its lines on EBITDA accounts (3000-7699, 7900-7999), with the sign that
removes the item from EBITDA. Only flags with confidence >= 0.7 adjust, and only for
vouchers inside the period.
"""

from collections import defaultdict
from decimal import Decimal

from diligens.models import (
    BridgeItem,
    EbitdaBridge,
    Flag,
    FlagCategory,
    KPIs,
    Ledger,
    Voucher,
)

ZERO = Decimal("0")
CONFIDENCE_THRESHOLD = 0.7

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


def _ebitda_effect(voucher: Voucher) -> Decimal:
    """Raw net of the voucher's EBITDA lines; adding it back removes the item from EBITDA."""
    return sum(
        (
            t.amount
            for t in voucher.transactions
            if t.account.isdigit()
            and (3000 <= int(t.account) <= 7699 or 7900 <= int(t.account) <= 7999)
        ),
        ZERO,
    )


def ebitda_bridge(kpis: KPIs, ledger: Ledger, flags: list[Flag]) -> EbitdaBridge:
    """LTM (or YTD when there is no LTM yet) EBITDA, adjusted by confident flags."""
    month = kpis.months[-1]
    ltm = kpis.ltm["ebitda"].values.get(month)
    if ltm is not None:
        basis, reported, period = "LTM", ltm, kpis.months[-12:]
    else:
        ytd = kpis.ytd["ebitda"].values
        start = next(m for m in kpis.months if ytd.get(m) is not None and m[:4] == month[:4])
        basis, reported, period = "YTD", ytd[month] or ZERO, kpis.months[kpis.months.index(start) :]
    in_period = set(period)

    vouchers = {v.ref: v for v in ledger.vouchers}
    groups: dict[tuple[FlagCategory, str], list[tuple[Flag, Decimal]]] = defaultdict(list)
    review: list[Flag] = []
    not_adjusted: list[Flag] = []
    out: list[Flag] = []
    for flag in flags:
        voucher = vouchers[flag.voucher_ref]
        month_of = f"{voucher.date.year}-{voucher.date.month:02d}"
        adjustment = ZERO
        if flag.category != "normal" and flag.confidence < CONFIDENCE_THRESHOLD:
            review.append(flag)
        elif flag.category in _NOT_ADJUSTING:
            not_adjusted.append(flag)
        elif flag.category in _ADJUSTING and month_of in in_period:
            adjustment = _ebitda_effect(voucher)
            groups[(flag.category, flag.account)].append((flag, adjustment))
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
                accounts=[account],
                voucher_refs=refs,
                amount=sum((a for _, a in members), ZERO),
                note=note,
                source_ref=(
                    f"Verifikat {shown}, EBITDA-konton på verifikaten, "
                    f"{period[0]} till {period[-1]}"
                ),
            )
        )

    return EbitdaBridge(
        basis=basis,
        month=month,
        period=[period[0], period[-1]],
        reported=reported,
        items=items,
        adjusted=reported + sum((i.amount for i in items), ZERO),
        review=review,
        not_adjusted=not_adjusted,
        flags=out,
    )

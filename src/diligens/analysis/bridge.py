"""Adjusted EBITDA bridge from flags, per docs/DOMAIN.md section 6 and docs/DECISIONS.md.

Adjustments are computed here from the vouchers, never taken from the LLM: the amount of a
flag is the net of its lines on EBITDA accounts, with the sign that removes the item from
EBITDA. Only flags with confidence >= 0.7 adjust, only for vouchers inside the period, and
only when the voucher has a single EBITDA account. A collective voucher (payroll batch,
daily sales journal) goes to review, since adding it back whole would overstate EBITDA.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal

from diligens.analysis.mapping import is_ebitda_account, is_revenue_account
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


def revenue_effect(voucher: Voucher) -> Decimal:
    """Raw net of the voucher's Nettoomsättning lines; adding it back removes it from revenue."""
    return sum((v for a, v in ebitda_lines(voucher).items() if is_revenue_account(a)), ZERO)


def _period(kpis: KPIs, ledger: Ledger) -> tuple[str, Decimal, Decimal, list[str]]:
    """Basis, EBITDA, Nettoomsättning and the months of the bridge period."""
    month = kpis.months[-1]
    ltm = kpis.ltm["ebitda"].values.get(month)
    if ltm is not None:
        revenue = kpis.ltm["nettoomsattning"].values.get(month) or ZERO
        return "LTM", ltm, revenue, kpis.months[-12:]
    year = next(fy for fy in ledger.fiscal_years if month in set(months_of(fy)))
    in_year = set(months_of(year))
    period = [m for m in kpis.months if m in in_year and m <= month]
    ytd = kpis.ytd
    return (
        "YTD",
        ytd["ebitda"].values[month] or ZERO,
        ytd["nettoomsattning"].values[month] or ZERO,
        period,
    )


_CANDIDATE_LABELS: dict[FlagCategory, str] = {
    **{c: label for c, (label, _) in _ADJUSTING.items()},
    "narstaende": "Närstående",
}
_THRESHOLD = f"{REVIEW_CONFIDENCE:.1f}".replace(".", ",")
_RELATED_NOTE = (
    "Närstående part, ej justerat. Bedöm om villkoren är marknadsmässiga; med Inkludera 1 "
    "läggs hela beloppet tillbaka."
)
_REVIEW_NOTE = f"Kräver granskning, konfidens under {_THRESHOLD}."


def _text(voucher: Voucher) -> str:
    """Voucher text without dates and numbers, so a monthly series reads as one item."""
    words = re.sub(r"[\d\-./:]+", " ", voucher.text).split()
    return re.sub(r"\s+,", ",", " ".join(words)).strip(" ,")[:60]


def _item(
    *,
    category: FlagCategory,
    label: str,
    account: str,
    members: list[Voucher],
    ledger: Ledger,
    period: list[str],
    note: str | None,
    included: bool,
) -> BridgeItem:
    name = ledger.accounts[account].name if account in ledger.accounts else account
    refs = [v.ref for v in members]
    shown = ", ".join(refs[:5]) + (f" m.fl. ({len(refs)} st)" if len(refs) > 5 else "")
    text = _text(members[0])
    return BridgeItem(
        category=category,
        label=f"{label}: {name}" + (f", {text}" if text else ""),
        accounts=sorted({a for v in members for a in ebitda_lines(v)}),
        voucher_refs=refs,
        amount=sum((ebitda_effect(v) for v in members), ZERO),
        revenue_amount=sum((revenue_effect(v) for v in members), ZERO),
        note=note,
        source_ref=(
            f"Verifikat {shown}, EBITDA-konton på verifikaten, {period[0]} till {period[-1]}"
        ),
        included=included,
    )


def with_selection(bridge: EbitdaBridge, included: Sequence[bool]) -> EbitdaBridge:
    """The bridge with the advisor's Inkludera choices, one per item then candidate (the
    order of the Excel bridge), and both totals recomputed from the chosen items."""
    rows = [*bridge.items, *bridge.candidates]
    if len(included) != len(rows):
        raise ValueError(f"Urvalet har {len(included)} värden men bryggan har {len(rows)} poster.")
    chosen = [
        r.model_copy(update={"included": bool(on)}) for r, on in zip(rows, included, strict=True)
    ]
    return bridge.model_copy(
        update={
            "items": chosen[: len(bridge.items)],
            "candidates": chosen[len(bridge.items) :],
            "adjusted": bridge.reported + sum((r.amount for r in chosen if r.included), ZERO),
            "adjusted_revenue": bridge.revenue
            + sum((r.revenue_amount for r in chosen if r.included), ZERO),
        }
    )


def ebitda_bridge(kpis: KPIs, ledger: Ledger, flags: list[Flag]) -> EbitdaBridge:
    """LTM (or YTD when there is no LTM yet) EBITDA, adjusted by confident flags.

    Related party items and low confidence items of an adjusting category are listed as
    candidates with included=False, so the advisor can switch them on in the Excel bridge.
    """
    basis, reported, revenue, period = _period(kpis, ledger)
    in_period = set(period)

    vouchers = {v.ref: v for v in ledger.vouchers}
    groups: dict[tuple[FlagCategory, str], list[Voucher]] = defaultdict(list)
    candidates: dict[tuple[str, FlagCategory, str], list[Voucher]] = defaultdict(list)
    review: list[Flag] = []
    not_adjusted: list[Flag] = []
    outside: list[Flag] = []
    out: list[Flag] = []
    for flag in flags:
        voucher = vouchers[flag.voucher_ref]
        single = len(ebitda_lines(voucher)) <= 1
        inside = month_key(voucher.date) in in_period
        adjustment = ZERO
        if flag.category != "normal" and flag.confidence < REVIEW_CONFIDENCE:
            review.append(flag)
            if flag.category in _CANDIDATE_LABELS and single and inside:
                candidates[("granskning", flag.category, flag.account)].append(voucher)
        elif flag.category in _NOT_ADJUSTING:
            not_adjusted.append(flag)
            if flag.category == "narstaende" and single and inside:
                candidates[("narstaende", flag.category, flag.account)].append(voucher)
        elif flag.category in _ADJUSTING:
            if not single:
                flag = flag.model_copy(update={"rationale": flag.rationale + COLLECTIVE_NOTE})
                review.append(flag)
            elif not inside:
                outside.append(flag)
            else:
                adjustment = ebitda_effect(voucher)
                groups[(flag.category, flag.account)].append(voucher)
        out.append(flag.model_copy(update={"ebitda_adjustment": adjustment}))

    items = [
        _item(
            category=category,
            label=_ADJUSTING[category][0],
            account=account,
            members=members,
            ledger=ledger,
            period=period,
            note=_ADJUSTING[category][1],
            included=True,
        )
        for (category, account), members in sorted(groups.items())
    ]
    listed = [
        _item(
            category=category,
            label=_CANDIDATE_LABELS[category],
            account=account,
            members=members,
            ledger=ledger,
            period=period,
            note=_RELATED_NOTE if kind == "narstaende" else _REVIEW_NOTE,
            included=False,
        )
        for (kind, category, account), members in sorted(candidates.items())
    ]

    return EbitdaBridge(
        basis=basis,
        month=period[-1],
        period=[period[0], period[-1]],
        reported=reported,
        items=items,
        adjusted=reported + sum((i.amount for i in items), ZERO),
        revenue=revenue,
        adjusted_revenue=revenue + sum((i.revenue_amount for i in items), ZERO),
        review=review,
        not_adjusted=not_adjusted,
        outside_period=outside,
        candidates=listed,
        flags=out,
    )

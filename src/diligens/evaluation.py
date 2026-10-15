"""Score flags against the facit of the synthetic demo company. Pure, used by the eval script."""

from pydantic import BaseModel

from diligens.analysis.bridge import CONFIDENCE_THRESHOLD
from diligens.models import Facit, Flag, PrefilterHit


class AnomalyScore(BaseModel):
    id: int
    description: str
    found: bool
    correct_vouchers: int
    total_vouchers: int
    in_prefilter: int
    categories: list[str]


class EvalReport(BaseModel):
    rows: list[AnomalyScore]
    found: int
    total: int
    precision: float | None
    confident_flags: int


def score_flags(
    facit: Facit, flags: list[Flag], hits: list[PrefilterHit] | None = None
) -> EvalReport:
    """An anomaly counts as found when at least one of its vouchers has a confident flag in an
    accepted category. Precision is the share of confident non-normal flags that sit on a
    planted voucher."""
    by_ref = {f.voucher_ref: f for f in flags}
    hit_refs = {h.voucher_ref for h in hits or []}
    confident = [
        f for f in flags if f.category != "normal" and f.confidence >= CONFIDENCE_THRESHOLD
    ]
    planted = {r for a in facit.anomalies for r in a.voucher_refs}

    rows = []
    for anomaly in facit.anomalies:
        accepted = {anomaly.category, *anomaly.also_accepted}
        on_refs = [by_ref[r] for r in anomaly.voucher_refs if r in by_ref]
        correct = [
            f for f in on_refs if f.category in accepted and f.confidence >= CONFIDENCE_THRESHOLD
        ]
        rows.append(
            AnomalyScore(
                id=anomaly.id,
                description=anomaly.description,
                found=bool(correct),
                correct_vouchers=len(correct),
                total_vouchers=len(anomaly.voucher_refs),
                in_prefilter=sum(r in hit_refs for r in anomaly.voucher_refs),
                categories=sorted({f.category for f in on_refs}),
            )
        )
    return EvalReport(
        rows=rows,
        found=sum(r.found for r in rows),
        total=len(rows),
        precision=(
            sum(f.voucher_ref in planted for f in confident) / len(confident) if confident else None
        ),
        confident_flags=len(confident),
    )

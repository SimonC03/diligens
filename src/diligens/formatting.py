"""Presentation formats for what the advisors see. Computation stays in Decimal elsewhere."""

from decimal import ROUND_HALF_UP, Decimal


def format_tsek(amount: Decimal) -> str:
    """SEK as TSEK with one decimal, space as thousands separator, comma as decimal mark."""
    tsek = (amount / 1000).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{tsek:,.1f}".replace(",", " ").replace(".", ",")

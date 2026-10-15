"""M3 agent tests. One test per acceptance criterion in docs/PLAN.md.

No test here calls the Anthropic API except the one marked `live`.
"""

import pytest

from diligens.agent.prefilter import prefilter
from diligens.analysis.mapping import map_accounts
from diligens.analysis.statements import monthly_statements
from diligens.models import Facit, Ledger
from diligens.sie.generator import build_demo_company


@pytest.fixture(scope="module")
def demo() -> tuple[Ledger, Facit]:
    return build_demo_company(42)


def test_prefilter_catches_planted_anomalies_and_reduces(demo: tuple[Ledger, Facit]) -> None:
    ledger, facit = demo

    hits = prefilter(ledger, monthly_statements(ledger, map_accounts(ledger)))

    by_ref = {h.voucher_ref: h for h in hits}
    assert len(by_ref) == len(hits), "one hit per voucher"
    for anomaly in facit.anomalies:
        missed = [r for r in anomaly.voucher_refs if r not in by_ref]
        assert not missed, (anomaly.id, missed)
    assert len(hits) <= 0.15 * len(ledger.vouchers)
    assert all(h.reasons and h.accounts for h in hits)

    reasons = {r for h in hits for r in h.reasons}
    for expected in (
        "Väsentligt belopp",
        "Återkommande motpart och belopp",
        "Ägarrelaterat konto eller text",
        "Avvikande månad på kontot",
        "Intäkt vid periodgräns",
        "Runt belopp",
        "Övrig rörelseintäkt",
    ):
        assert any(r.startswith(expected) for r in reasons), expected

    # Deterministic: same input, same hits in the same order.
    again = prefilter(ledger, monthly_statements(ledger, map_accounts(ledger)))
    assert again == hits

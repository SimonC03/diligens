"""M3 agent tests. One test per acceptance criterion in docs/PLAN.md.

No test here calls the Anthropic API except the one marked `live`.
"""

import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anthropic import Anthropic

from diligens.agent.flagging import flag_vouchers
from diligens.agent.prefilter import prefilter
from diligens.analysis.mapping import map_accounts
from diligens.analysis.statements import monthly_statements
from diligens.models import Facit, Ledger, PrefilterHit
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


class FakeMessages:
    """Stands in for client.messages. `script` decides each reply from the refs it gets."""

    def __init__(self, script: Callable[[int, list[str]], dict[str, Any]]) -> None:
        self.script = script
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        content = kwargs["messages"][0]["content"]
        refs = re.findall(r'"ref": "(\w+)"', content)
        reply = self.script(len(self.calls), refs)
        parsed = kwargs["output_format"].model_validate(reply["output"])
        return SimpleNamespace(
            parsed_output=parsed,
            stop_reason=reply.get("stop_reason", "end_turn"),
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=50,
                cache_read_input_tokens=80,
                cache_creation_input_tokens=0,
            ),
        )


class FakeClient:
    def __init__(self, script: Callable[[int, list[str]], dict[str, Any]]) -> None:
        self.messages = FakeMessages(script)


def _all_normal(refs: list[str], text: str = "Löpande kostnad utan avvikelse.") -> dict[str, Any]:
    flags = [
        {"voucher_ref": r, "category": "normal", "rationale": text, "confidence": 0.9} for r in refs
    ]
    return {"output": {"flags": flags}}


def test_flagging_batches_validates_and_retries(demo: tuple[Ledger, Facit]) -> None:
    ledger, _ = demo
    hits = [
        PrefilterHit(voucher_ref=v.ref, accounts=["3041"], reasons=["Testregel"])
        for v in ledger.vouchers
        if v.series == "B"
    ][:120]

    def script(call: int, refs: list[str]) -> dict[str, Any]:
        if call == 1:  # first batch: one voucher missing
            return _all_normal(refs[1:])
        if call == 3:  # second batch: a rationale with digits is a made up number
            return _all_normal(refs, "Fakturan på 125 000 kr är normal.")
        if call >= 5:  # third batch never gets it right
            return {"output": {"flags": []}, "stop_reason": "refusal"}
        return _all_normal(refs)

    client = FakeClient(script)
    result = flag_vouchers(hits, ledger, cast(Anthropic, client), model="claude-test")

    calls = client.messages.calls
    assert len(calls) == 2 + 2 + 3  # retry, retry, two retries then fallback
    first = calls[0]
    assert first["model"] == "claude-test"
    assert first["output_config"] == {"effort": "medium"}
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "6550 Konsultarvoden" in first["system"][-1]["text"]  # chart of accounts
    assert len(re.findall(r'"ref": ', first["messages"][0]["content"])) == 50
    assert "saknas" in calls[1]["messages"][0]["content"]  # error fed back on retry
    assert "siffror" in calls[3]["messages"][0]["content"]

    flags = {f.voucher_ref: f for f in result.flags}
    assert len(result.flags) == 120 and set(flags) == {h.voucher_ref for h in hits}
    assert all(flags[h.voucher_ref].category == "normal" for h in hits[:100])
    for h in hits[100:]:  # third batch fell back to review
        assert flags[h.voucher_ref].category == "avvikelse"
        assert flags[h.voucher_ref].needs_review
    assert result.failed_batches == 1 and result.calls == 7

    # Account and amount come from the voucher, never from the model.
    voucher = next(v for v in ledger.vouchers if v.ref == hits[0].voucher_ref)
    revenue = next(t for t in voucher.transactions if t.account == "3041")
    assert (flags[voucher.ref].account, flags[voucher.ref].amount) == ("3041", revenue.amount)

    offline = flag_vouchers(hits[:3], ledger, None)
    assert offline.calls == 0
    assert all(f.category == "avvikelse" and f.needs_review for f in offline.flags)
    assert "Testregel" in offline.flags[0].rationale

"""M3 agent tests. One test per acceptance criterion in docs/PLAN.md.

No test here calls the Anthropic API except the one marked `live`.
"""

import importlib.util
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anthropic import Anthropic
from typer.testing import CliRunner

from diligens.agent.flagging import flag_vouchers
from diligens.agent.prefilter import prefilter
from diligens.agent.questions import dd_questions
from diligens.analysis.bridge import ebitda_bridge
from diligens.analysis.kpis import compute_kpis
from diligens.analysis.mapping import map_accounts
from diligens.analysis.statements import monthly_statements
from diligens.cli import app
from diligens.config import Settings
from diligens.evaluation import score_flags
from diligens.models import Facit, Flag, FlagCategory, Ledger, PrefilterHit
from diligens.pipeline import run_analysis
from diligens.sie.generator import build_demo_company, generate_demo_company
from diligens.sie.parser import parse_sie

FIXTURES = Path(__file__).parent / "fixtures"


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


class FakeQuestionMessages(FakeMessages):
    """Like FakeMessages but reads the question group ids from the request."""

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        ids = re.findall(r'"id": "(\w+)"', kwargs["messages"][0]["content"])
        reply = self.script(len(self.calls), ids)
        parsed = kwargs["output_format"].model_validate(reply["output"])
        return SimpleNamespace(
            parsed_output=parsed,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=10, cache_read_input_tokens=0),
        )


class FakeQuestionClient:
    def __init__(self, script: Callable[[int, list[str]], dict[str, Any]]) -> None:
        self.messages = FakeQuestionMessages(script)


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


def _flags_on(
    ledger: Ledger, refs: list[str], category: FlagCategory, confidence: float
) -> list[Flag]:
    by_ref = {v.ref: v for v in ledger.vouchers}
    out = []
    for ref in refs:
        main = max(
            (t for t in by_ref[ref].transactions if t.account[0] in "34567"),
            key=lambda t: abs(t.amount),
        )
        out.append(
            Flag(
                voucher_ref=ref,
                account=main.account,
                amount=main.amount,
                category=category,
                rationale="Test.",
                confidence=confidence,
            )
        )
    return out


def test_bridge_uses_only_confident_flags(demo: tuple[Ledger, Facit]) -> None:
    ledger, facit = demo
    a = {x.id: x for x in facit.anomalies}
    kpis = compute_kpis(monthly_statements(ledger, map_accounts(ledger)))
    rent = next(v.ref for v in ledger.vouchers if v.text.startswith("Hyra kontor 2025-03"))
    stray = next(v.ref for v in ledger.vouchers if v.text.startswith("Annonsering"))
    flags = (
        _flags_on(ledger, a[1].voucher_refs, "agarrelaterad", 0.9)  # 12 of 24 in LTM
        + _flags_on(ledger, a[2].voucher_refs, "engangspost", 0.95)
        + _flags_on(ledger, a[3].voucher_refs, "agarrelaterad", 0.6)  # below threshold
        + _flags_on(ledger, a[4].voucher_refs, "periodisering", 0.85)
        + _flags_on(ledger, a[5].voucher_refs, "engangspost", 0.9)  # 2024, outside LTM
        + _flags_on(ledger, a[6].voucher_refs, "engangspost", 0.8)
        + _flags_on(ledger, [stray], "narstaende", 0.9)  # question, no adjustment
        + _flags_on(ledger, [rent], "normal", 0.95)
    )

    bridge = ebitda_bridge(kpis, ledger, flags)

    assert bridge.basis == "LTM" and bridge.month == "2025-12"
    assert bridge.reported == kpis.ltm["ebitda"].values["2025-12"]
    by_cat = {(i.category, i.accounts[0]): i for i in bridge.items}
    assert by_cat[("agarrelaterad", "6550")].amount == Decimal("540000.00")
    assert len(by_cat[("agarrelaterad", "6550")].voucher_refs) == 12
    assert "marknadsmässig" in (by_cat[("agarrelaterad", "6550")].note or "")
    assert sum(i.amount for i in bridge.items if i.category == "engangspost") == Decimal(
        "530000.00"
    )  # move 380 + recruitment 150; the 2024 insurance payout is outside LTM
    assert by_cat[("periodisering", "3041")].amount == Decimal("-600000.00")
    assert all(i.source_ref and i.voucher_refs for i in bridge.items)
    assert bridge.adjusted == bridge.reported + Decimal("470000.00")
    assert bridge.adjusted == bridge.reported + sum(i.amount for i in bridge.items)

    assert {f.voucher_ref for f in bridge.review} == set(a[3].voucher_refs)
    assert [f.voucher_ref for f in bridge.not_adjusted] == [stray]
    adjusted = {f.voucher_ref: f.ebitda_adjustment for f in bridge.flags}
    assert adjusted[a[1].voucher_refs[-1]] == Decimal("45000.00")
    assert adjusted[a[1].voucher_refs[0]] == 0  # January 2024, outside LTM
    assert adjusted[a[3].voucher_refs[-1]] == 0  # review, not in the bridge

    # Fewer than 12 months: the bridge falls back to year to date.
    minimal = parse_sie(FIXTURES / "minimal.se")
    small = ebitda_bridge(
        compute_kpis(monthly_statements(minimal, map_accounts(minimal))),
        minimal,
        _flags_on(minimal, ["B2"], "engangspost", 0.9),
    )
    assert (small.basis, small.reported, small.adjusted) == (
        "YTD",
        Decimal("10000.00"),
        Decimal("50000.00"),
    )


def test_dd_questions_reference_account_voucher_and_amount(demo: tuple[Ledger, Facit]) -> None:
    ledger, facit = demo
    a = {x.id: x for x in facit.anomalies}
    rent = next(v.ref for v in ledger.vouchers if v.text.startswith("Hyra kontor 2025-03"))
    flags = (
        _flags_on(ledger, a[1].voucher_refs, "agarrelaterad", 0.9)
        + _flags_on(ledger, a[4].voucher_refs, "periodisering", 0.85)
        + _flags_on(ledger, a[3].voucher_refs, "agarrelaterad", 0.6)
        + _flags_on(ledger, [rent], "normal", 0.95)  # no question for normal items
    )

    def script(call: int, ids: list[str]) -> dict[str, Any]:
        questions = [{"id": i, "text": "Vad avser posterna och återkommer de?"} for i in ids]
        if call == 1:
            questions[0]["text"] = "Vad avser de 45 000 kr per månad?"  # digits: rejected
        return {"output": {"questions": questions}}

    client = FakeQuestionClient(script)
    questions = dd_questions(flags, ledger, cast(Anthropic, client), model="claude-test")

    assert len(client.messages.calls) == 2
    assert "siffror" in client.messages.calls[1]["messages"][0]["content"]
    assert len(questions) == 3  # owner fee (6550), car (5615), early invoice (3041)
    owner = next(q for q in questions if q.accounts == ["6550"])
    assert owner.voucher_refs == a[1].voucher_refs
    assert owner.amount == Decimal("1080000.00")
    assert owner.text.startswith("Vad avser posterna och återkommer de?")
    assert "konto 6550" in owner.text and "1 080,0 TSEK" in owner.text
    for q in questions:
        assert q.voucher_refs and q.accounts and q.amount is not None
        question_part = q.text.split(" (konto")[0]
        assert not re.search(r"\d", question_part)

    offline = dd_questions(flags, ledger, None)
    assert [q.accounts for q in offline] == [q.accounts for q in questions]
    assert all(q.text.split(" (konto")[0].endswith("?") for q in offline)

    def never(call: int, ids: list[str]) -> dict[str, Any]:
        return {"output": {"questions": []}}

    fallback = dd_questions(flags, ledger, cast(Anthropic, FakeQuestionClient(never)))
    assert [q.text for q in fallback] == [q.text for q in offline]


def test_no_llm_runs_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("--no-llm must not create an Anthropic client")

    monkeypatch.setattr("anthropic.Anthropic", no_client)
    sie = tmp_path / "demo_bolag.se"
    generate_demo_company(42, sie)

    result = CliRunner().invoke(app, ["analyze", str(sie), "--out", str(tmp_path), "--no-llm"])

    assert result.exit_code == 0, result.output
    for expected in (
        "Demobolaget Konsult AB",
        "Avstämning: OK",
        "EBITDA LTM",
        "Justerad EBITDA",
        "Kräver granskning",
        "DD-frågor",
    ):
        assert expected in result.output, expected

    analysis = run_analysis(sie, use_llm=False)
    assert not analysis.llm_used and analysis.reconciliation.ok
    assert len(analysis.flags) == len(analysis.prefilter_hits) > 0
    assert all(f.category == "avvikelse" and f.needs_review for f in analysis.flags)
    assert analysis.bridge.adjusted == analysis.bridge.reported  # nothing confident offline
    assert analysis.questions and all(q.voucher_refs for q in analysis.questions)

    no_key = Settings(_env_file=None, anthropic_api_key=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        run_analysis(sie, use_llm=True, settings=no_key)


def test_eval_scores_flags_against_facit(demo: tuple[Ledger, Facit], tmp_path: Path) -> None:
    ledger, facit = demo
    perfect = [
        f
        for anomaly in facit.anomalies
        for f in _flags_on(ledger, anomaly.voucher_refs, anomaly.category, 0.9)
    ]
    report = score_flags(facit, perfect)
    assert (report.found, report.total, report.precision) == (6, 6, 1.0)

    rent = next(v.ref for v in ledger.vouchers if v.text.startswith("Hyra kontor 2025-03"))
    a = {x.id: x for x in facit.anomalies}
    mixed = (
        _flags_on(ledger, a[1].voucher_refs, "narstaende", 0.8)  # also accepted for #1
        + _flags_on(ledger, a[2].voucher_refs, "engangspost", 0.9)
        + _flags_on(ledger, a[3].voucher_refs, "agarrelaterad", 0.9)
        + _flags_on(ledger, a[4].voucher_refs, "periodisering", 0.5)  # too unsure to count
        + _flags_on(ledger, a[5].voucher_refs, "engangspost", 0.9)
        + _flags_on(ledger, a[6].voucher_refs, "engangspost", 0.9)
        + _flags_on(ledger, [rent], "engangspost", 0.9)  # false positive
    )
    report = score_flags(facit, mixed)
    assert report.found == 5 and not next(r for r in report.rows if r.id == 4).found
    assert report.precision == pytest.approx(55 / 56)

    script = importlib.util.spec_from_file_location(
        "eval_flags", Path(__file__).parents[1] / "scripts" / "eval_flags.py"
    )
    assert script and script.loader
    module = importlib.util.module_from_spec(script)
    script.loader.exec_module(module)
    # Offline everything is "avvikelse", so nothing is found; the plumbing still runs.
    assert module.main(["--no-llm", "--out", str(tmp_path)]) == 1
    assert (tmp_path / "facit.json").exists()


@pytest.mark.live
def test_live_flagging_returns_valid_flags(demo: tuple[Ledger, Facit]) -> None:
    """Real API call on three demo vouchers. Run with: uv run pytest -m live"""
    settings = Settings()
    if settings.anthropic_api_key is None:
        pytest.skip("ANTHROPIC_API_KEY saknas")
    ledger, facit = demo
    a = {x.id: x for x in facit.anomalies}
    rent = next(v.ref for v in ledger.vouchers if v.text.startswith("Hyra kontor 2025-03"))
    refs = [a[1].voucher_refs[-1], a[6].voucher_refs[0], rent]
    hits = [PrefilterHit(voucher_ref=r, accounts=[], reasons=["Livetest"]) for r in refs]
    client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())

    result = flag_vouchers(hits, ledger, client, model=settings.diligens_model)

    assert result.failed_batches == 0 and result.calls >= 1
    assert [f.voucher_ref for f in result.flags] == refs
    assert all(f.rationale and not re.search(r"\d", f.rationale) for f in result.flags)

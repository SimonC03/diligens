"""M1 generator tests. One test per acceptance criterion in docs/PLAN.md."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from diligens.models import Facit, Ledger
from diligens.sie.generator import generate_demo_company
from diligens.sie.parser import parse_sie

SEED = 42


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Facit, Ledger]:
    out = tmp_path_factory.mktemp("demo") / "demo_bolag.se"
    facit = generate_demo_company(SEED, out)
    return out, facit, parse_sie(out)


def _net_revenue(ledger: Ledger, year: int) -> Decimal:
    return -sum(
        (
            t.amount
            for v in ledger.vouchers
            if v.date.year == year
            for t in v.transactions
            if 3000 <= int(t.account) <= 3699
        ),
        Decimal("0"),
    )


def test_generator_builds_service_company(demo: tuple[Path, Facit, Ledger], tmp_path: Path) -> None:
    out, _, ledger = demo

    years = {fy.index: (fy.start, fy.end) for fy in ledger.fiscal_years}
    assert years == {
        0: (dt.date(2025, 1, 1), dt.date(2025, 12, 31)),
        -1: (dt.date(2024, 1, 1), dt.date(2024, 12, 31)),
    }
    assert all(dt.date(2024, 1, 1) <= v.date <= dt.date(2025, 12, 31) for v in ledger.vouchers)
    assert {(v.date.year, v.date.month) for v in ledger.vouchers} == {
        (y, m) for y in (2024, 2025) for m in range(1, 13)
    }
    assert 1900 <= len(ledger.vouchers) <= 2100
    for year in (2024, 2025):
        assert Decimal("36000000") <= _net_revenue(ledger, year) <= Decimal("44000000")

    again = tmp_path / "again.se"
    generate_demo_company(SEED, again)
    assert again.read_bytes() == out.read_bytes()
    other = tmp_path / "other.se"
    generate_demo_company(SEED + 1, other)
    assert other.read_bytes() != out.read_bytes()


def _amount_on(ledger: Ledger, refs: list[str], accounts: list[str]) -> Decimal:
    wanted = set(refs)
    return sum(
        (
            t.amount
            for v in ledger.vouchers
            if v.ref in wanted
            for t in v.transactions
            if t.account in accounts
        ),
        Decimal("0"),
    )


def test_generator_plants_domain_anomalies(demo: tuple[Path, Facit, Ledger]) -> None:
    out, facit, ledger = demo

    on_disk = Facit.model_validate_json((out.parent / "facit.json").read_text(encoding="utf-8"))
    assert on_disk == facit
    assert facit.seed == SEED
    assert [(a.id, a.category) for a in facit.anomalies] == [
        (1, "agarrelaterad"),
        (2, "engangspost"),
        (3, "agarrelaterad"),
        (4, "periodisering"),
        (5, "engangspost"),
        (6, "engangspost"),
    ]
    assert facit.anomalies[0].also_accepted == ["narstaende"]

    by_ref = {v.ref: v for v in ledger.vouchers}
    for anomaly in facit.anomalies:
        assert anomaly.voucher_refs and all(r in by_ref for r in anomaly.voucher_refs)
        assert _amount_on(ledger, anomaly.voucher_refs, anomaly.accounts) == anomaly.total_amount
    a = {x.id: x for x in facit.anomalies}

    owner = [by_ref[r] for r in a[1].voucher_refs]
    assert len(owner) == 24 and a[1].accounts == ["6550"]
    assert all("Ägarbolaget AB" in v.text for v in owner)
    assert all(_amount_on(ledger, [v.ref], ["6550"]) == Decimal("45000.00") for v in owner)

    move = [by_ref[r] for r in a[2].voucher_refs]
    assert len(move) == 5 and len({(v.date.year, v.date.month) for v in move}) == 1
    assert a[2].accounts == ["5010", "5410"] and a[2].total_amount == Decimal("380000.00")

    car = [by_ref[r] for r in a[3].voucher_refs]
    assert len(car) == 24 and a[3].accounts == ["5615"]
    assert all(_amount_on(ledger, [v.ref], ["5615"]) == Decimal("8000.00") for v in car)

    (late,) = [by_ref[r] for r in a[4].voucher_refs]
    assert late.date == dt.date(2025, 12, 30) and "januari 2026" in late.text
    assert a[4].accounts == ["3041"] and a[4].total_amount == Decimal("-600000.00")
    receivable = _amount_on(ledger, [late.ref], ["1510"])
    assert not any(
        t.account == "1510" and t.amount == -receivable
        for v in ledger.vouchers
        if v.date > late.date
        for t in v.transactions
    )

    assert a[5].accounts == ["3990"] and a[5].total_amount == Decimal("-250000.00")
    assert a[6].accounts == ["6550"] and a[6].total_amount == Decimal("150000.00")

    # Exactly these anomalies: the anomaly accounts are not used anywhere else.
    planted = {r for x in facit.anomalies for r in x.voucher_refs}
    for v in ledger.vouchers:
        if any(t.account in {"5615", "3990", "6550"} for t in v.transactions):
            assert v.ref in planted, v.ref

"""M1 parser tests. One test per acceptance criterion in docs/PLAN.md."""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from diligens.sie.parser import parse_sie

FIXTURES = Path(__file__).parent / "fixtures"


def _load_expected() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES / "minimal_expected.json").read_text(encoding="utf-8")
    )
    return data


def test_parse_minimal_matches_expected() -> None:
    expected = _load_expected()
    ledger = parse_sie(FIXTURES / "minimal.se")

    assert ledger.company_name == expected["company_name"]
    assert ledger.org_nr == expected["org_nr"]
    assert [
        {"index": fy.index, "start": fy.start.isoformat(), "end": fy.end.isoformat()}
        for fy in ledger.fiscal_years
    ] == expected["fiscal_years"]
    assert len(ledger.accounts) == expected["account_count"]
    assert len(ledger.vouchers) == expected["voucher_count"]
    assert sum(len(v.transactions) for v in ledger.vouchers) == expected["transaction_count"]
    assert [v.ref for v in ledger.vouchers] == expected["voucher_refs"]

    b2 = next(v for v in ledger.vouchers if v.ref == "B2")
    exp_b2 = expected["voucher_b2"]
    assert b2.text == exp_b2["text"]
    assert b2.date == dt.date.fromisoformat(exp_b2["date"])
    assert b2.reg_date == dt.date.fromisoformat(exp_b2["reg_date"])
    last = b2.transactions[-1]
    exp_last = exp_b2["last_transaction"]
    assert last.account == exp_last["account"]
    assert last.amount == Decimal(exp_last["amount"])
    assert last.date == dt.date.fromisoformat(exp_last["date"])
    assert last.text == exp_last["text"]

    def as_dict(balances: list[Any]) -> dict[str, Decimal]:
        return {b.account: b.amount for b in balances if b.year_index == 0}

    def exp_dict(raw: dict[str, str]) -> dict[str, Decimal]:
        return {k: Decimal(v) for k, v in raw.items()}

    assert as_dict(ledger.opening_balances) == exp_dict(expected["opening_balances"])
    assert as_dict(ledger.closing_balances) == exp_dict(expected["closing_balances"])
    assert as_dict(ledger.results) == exp_dict(expected["results"])

    for needle in expected["warnings_contain"]:
        assert any(needle in w for w in ledger.warnings)

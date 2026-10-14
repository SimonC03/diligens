"""M1 parser tests. One test per acceptance criterion in docs/PLAN.md."""

import datetime as dt
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from diligens.models import Balance, FiscalYear, Transaction, Voucher
from diligens.sie.parser import SieParseError, check_integrity, parse_sie

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


ALL_TAGS_SIE = """#FLAGGA 0
#PROGRAM "Testprogram" 2.1
#FORMAT PC8
#GEN 20260102
#SIETYP 4
#FNAMN "Ölands Väg & Bygg AB"
#ORGNR 999999-9999
#RAR 0 20250101 20251231
#RAR -1 20240101 20241231
#KPTYP BAS2014
#KONTO 1930 "Företagskonto"
#KONTO 3041 "Försäljning tjänster"
#KTYP 1930 T
#KTYP 3041 I
#IB 0 1930 100.00
#IB -1 1930 0.00
#UB 0 1930 -900.00
#UB -1 1930 100.00
#RES 0 3041 1000.00
#PSALDO 0 202501 3041 {} 1000.00
#PSALDO 0 202501 3041 {1 "100"} 1000.00
#VER A 7 20250110 "Text med \\"citat\\" i"
{
#TRANS 3041 {1 "100"} 1000.00
#TRANS 1930 {} -600.00 20250111
#TRANS 1930 {} -400.00 20250112 "Rad med \\"citat\\" och åäö"
#RTRANS 1930 {} -400.00 20250112 "Borttagen rad"
#TRANS 1930 {} 0.00 20250112 "Kvantitet och sign" 2 "AB"
}
"""


def _write_cp437(path: Path, text: str) -> Path:
    path.write_bytes(text.replace("\n", "\r\n").encode("cp437"))
    return path


def test_parser_supports_all_domain_tags(tmp_path: Path) -> None:
    ledger = parse_sie(_write_cp437(tmp_path / "all.se", ALL_TAGS_SIE))

    assert ledger.program == "Testprogram"
    assert ledger.generated == dt.date(2026, 1, 2)
    assert ledger.chart_type == "BAS2014"
    assert ledger.company_name == "Ölands Väg & Bygg AB"
    assert [fy.index for fy in ledger.fiscal_years] == [0, -1]
    assert ledger.fiscal_years[1].start == dt.date(2024, 1, 1)
    assert ledger.accounts["1930"].name == "Företagskonto"
    assert ledger.accounts["1930"].type == "T"
    assert ledger.accounts["3041"].type == "I"
    assert {(b.year_index, b.amount) for b in ledger.opening_balances} == {
        (0, Decimal("100.00")),
        (-1, Decimal("0.00")),
    }
    assert len(ledger.closing_balances) == 2
    assert ledger.results[0].amount == Decimal("1000.00")
    total, by_object = ledger.period_balances
    assert (total.year_index, total.period, total.account, total.amount) == (
        0,
        "202501",
        "3041",
        Decimal("1000.00"),
    )
    assert total.objects == [] and by_object.objects == [("1", "100")]

    (voucher,) = ledger.vouchers
    assert voucher.ref == "A7"
    assert voucher.text == 'Text med "citat" i'
    assert voucher.reg_date is None
    with_objects, date_only, date_and_text, with_quantity = voucher.transactions
    assert with_objects.objects == [("1", "100")]
    assert with_objects.date is None and with_objects.text is None
    assert date_only.date == dt.date(2025, 1, 11) and date_only.text is None
    assert date_and_text.text == 'Rad med "citat" och åäö'
    assert with_quantity.text == "Kvantitet och sign"
    assert ledger.warnings == []

    wrong_type = _write_cp437(tmp_path / "typ3.se", ALL_TAGS_SIE.replace("#SIETYP 4", "#SIETYP 3"))
    with pytest.raises(SieParseError, match="SIE typ 3"):
        parse_sie(wrong_type)

    # Malformed records fail with a line number; the header takes lines 1 and 2.
    header = "#SIETYP 4\n#RAR 0 20250101 20251231\n"
    ver = '#VER A 1 20250101 "x"\n'
    malformed = {
        "rad 3: ogiltigt datum": '#VER A 1 2025013X "x"\n{\n}\n',
        "rad 6: ogiltigt belopp": ver + "{\n#TRANS 1930 {} 1.00\n#TRANS 3041 {} -1,00\n}\n",
        "rad 5: objektlistan": ver + "{\n#TRANS 1930 {1} 1.00\n}\n",
        "rad 3: '}' utan öppnat": "}\n",
        "rad 3: #VER saknar": ver + '#KONTO 1930 "x"\n',
        "rad 3: #VER avslutas inte": ver + "{\n#TRANS 1930 {} 0.00\n",
        "rad 3: #VER avslutas inte med": ver + "{\n#TRANS 1930 {} 0.00\n" + ver,
        "rad 3: #TRANS utanför": "#TRANS 1930 {} 1.00\n",
        "rad 3: ogiltigt årsindex": "#IB x 1930 1.00\n",
        "rad 3: citattecken": '#FNAMN "Bolaget AB\n',
    }
    for message, body in malformed.items():
        bad = _write_cp437(tmp_path / "bad.se", header + body)
        with pytest.raises(SieParseError, match=re.escape(message)):
            parse_sie(bad)


def test_integrity_check_returns_violations() -> None:
    ledger = parse_sie(FIXTURES / "minimal.se")
    assert check_integrity(ledger) == []

    # A standard export carries prior year balances without its vouchers; not a violation.
    prior_year = FiscalYear(index=-1, start=dt.date(2024, 1, 1), end=dt.date(2024, 12, 31))
    with_prior = ledger.model_copy(
        update={
            "fiscal_years": [*ledger.fiscal_years, prior_year],
            "closing_balances": [
                *ledger.closing_balances,
                Balance(year_index=-1, account="1930", amount=Decimal("100000.00")),
            ],
        }
    )
    assert check_integrity(with_prior) == []

    def replace_balance(balances: list[Balance], account: str, amount: str) -> list[Balance]:
        return [
            b.model_copy(update={"amount": Decimal(amount)}) if b.account == account else b
            for b in balances
        ]

    # B1: 5010 20000 -> 20100 unbalances the voucher; RES 5010 follows so only the voucher fails.
    vouchers = []
    for v in ledger.vouchers:
        if v.ref == "B1":
            first = v.transactions[0].model_copy(update={"amount": Decimal("20100.00")})
            v = v.model_copy(update={"transactions": [first, *v.transactions[1:]]})
        vouchers.append(v)
    broken = ledger.model_copy(
        update={
            "vouchers": vouchers,
            "closing_balances": replace_balance(ledger.closing_balances, "1930", "145001.00"),
            "results": replace_balance(
                replace_balance(ledger.results, "5010", "20100.00"), "6550", "41000.00"
            ),
        }
    )

    issues = check_integrity(broken)

    assert sorted((i.kind, i.ref, i.expected, i.actual) for i in issues) == [
        ("ib_trans_ub_mismatch", "1930", Decimal("145001.00"), Decimal("145000.00")),
        ("trans_res_mismatch", "6550", Decimal("41000.00"), Decimal("40000.00")),
        # Sum of UB (10 001) must equal minus the result (-(-8 900)).
        ("ub_res_mismatch", "RAR 0", Decimal("8900.00"), Decimal("10001.00")),
        ("unbalanced_voucher", "B1", Decimal("0"), Decimal("100.00")),
    ]

    # Amounts that no reconciliation would catch: outside every #RAR, or outside class 1-8.
    stray = Voucher(
        series="Z",
        number="1",
        date=dt.date(2026, 1, 5),
        transactions=[
            Transaction(account="1930", amount=Decimal("500.00")),
            Transaction(account="9010", amount=Decimal("-500.00")),
        ],
    )
    issues = check_integrity(ledger.model_copy(update={"vouchers": [*ledger.vouchers, stray]}))
    assert sorted((i.kind, i.ref, i.actual) for i in issues) == [
        ("unknown_account_class", "9010", Decimal("-500.00")),
        ("voucher_outside_fiscal_year", "Z1", Decimal("500.00")),
    ]

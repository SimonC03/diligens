"""M2 analysis tests. One test per acceptance criterion in docs/PLAN.md."""

import datetime as dt
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from diligens.analysis.kpis import compute_kpis
from diligens.analysis.mapping import map_accounts
from diligens.analysis.statements import monthly_statements
from diligens.models import Account, KPIs, Ledger, StatementLine, Transaction, Voucher
from diligens.sie.generator import build_demo_company
from diligens.sie.parser import parse_sie

FIXTURES = Path(__file__).parent / "fixtures"
PAYABLES = "Leverantörsskulder och kortfristiga skulder till kreditinstitut"


def test_bas_mapping_returns_unmapped() -> None:
    ledger = parse_sie(FIXTURES / "minimal.se")

    mapping = map_accounts(ledger)

    assert {a: (m.line, m.statement) for a, m in mapping.accounts.items()} == {
        "1510": ("Kundfordringar", "BR"),
        "1930": ("Kassa och bank", "BR"),
        "2081": ("Eget kapital", "BR"),
        "2440": (PAYABLES, "BR"),
        "2611": ("Moms och punktskatter", "BR"),
        "2641": ("Moms och punktskatter", "BR"),
        "3041": ("Nettoomsättning", "RR"),
        "5010": ("Övriga externa kostnader", "RR"),
        "6550": ("Övriga externa kostnader", "RR"),
        "7010": ("Personalkostnader", "RR"),
    }
    assert mapping.unmapped == []

    boundaries = {
        "1000": "Anläggningstillgångar",
        "1399": "Anläggningstillgångar",
        "1400": "Lager",
        "1999": "Kassa och bank",
        "2000": "Eget kapital",
        "2999": "Upplupna kostnader och förutbetalda intäkter",
        "3000": "Nettoomsättning",
        "3699": "Nettoomsättning",
        "3700": "Övriga rörelseintäkter",
        "3740": "Övriga rörelseintäkter",
        "4999": "Råvaror, handelsvaror och underentreprenader",
        "6999": "Övriga externa kostnader",
        "7699": "Personalkostnader",
        "7700": "Av och nedskrivningar",
        "7899": "Av och nedskrivningar",
        "7900": "Övriga rörelsekostnader",
        "8799": "Finansiella poster",
        "8899": "Bokslutsdispositioner",
        "8999": "Skatt och årets resultat",
    }
    names = [*boundaries, "0100", "9999", "ABC"]
    with_edges = ledger.model_copy(
        update={"accounts": {n: Account(number=n, name="x") for n in names}}
    )

    mapped = map_accounts(with_edges)

    for number, line in boundaries.items():
        assert mapped.accounts[number].line == line, number
    # Accounts only seen in transactions are mapped too.
    assert "3041" in mapped.accounts
    assert mapped.unmapped == ["0100", "9999", "ABC"]


@pytest.fixture(scope="module")
def minimal() -> Ledger:
    return parse_sie(FIXTURES / "minimal.se")


@pytest.fixture(scope="module")
def demo() -> Ledger:
    ledger, _ = build_demo_company(42)
    return ledger


def _line(lines: list[StatementLine], name: str) -> StatementLine:
    return next(line for line in lines if line.line == name)


def test_monthly_statements(minimal: Ledger, demo: Ledger) -> None:
    st = monthly_statements(minimal, map_accounts(minimal))

    assert st.months == [f"2025-{m:02d}" for m in range(1, 13)]
    revenue = _line(st.income, "Nettoomsättning")
    assert revenue.values["2025-01"] == Decimal("-100000.00")
    assert revenue.values["2025-02"] == 0
    assert revenue.accounts == ["3041"] and "3000–3699" in revenue.source_ref
    external = _line(st.income, "Övriga externa kostnader")
    assert (external.values["2025-02"], external.values["2025-03"]) == (20000, 40000)
    assert _line(st.income, "Personalkostnader").values["2025-03"] == 30000
    assert st.account_movements["3041"]["2025-01"] == Decimal("-100000.00")

    cash = _line(st.balance, "Kassa och bank")
    assert [cash.values[m] for m in ("2025-01", "2025-03", "2025-12")] == [225000, 145000, 145000]
    assert _line(st.balance, "Kundfordringar").values["2025-01"] == 0
    assert _line(st.balance, "Moms och punktskatter").values["2025-03"] == Decimal("-10000.00")
    assert _line(st.balance, "Årets resultat (beräknat)").values["2025-03"] == -10000
    assert st.account_balances["1930"]["2025-03"] == Decimal("145000.00")

    # BR plus the computed result balances every month, also across two fiscal years.
    for ledger in (minimal, demo):
        statements = monthly_statements(ledger, map_accounts(ledger))
        for month in statements.months:
            assert sum(line.values[month] for line in statements.balance) == 0, month
    demo_st = monthly_statements(demo, map_accounts(demo))
    assert len(demo_st.months) == 24 and demo_st.months[0] == "2024-01"
    # The 2024 result is closed to equity, so the computed result restarts in 2025.
    assert _line(demo_st.balance, "Årets resultat (beräknat)").values["2024-12"] == 0


def _kpis(ledger: Ledger) -> KPIs:
    return compute_kpis(monthly_statements(ledger, map_accounts(ledger)))


def test_kpis(minimal: Ledger, demo: Ledger) -> None:
    expected = json.loads((FIXTURES / "minimal_expected.json").read_text(encoding="utf-8"))
    kpis = _kpis(minimal)

    for name, value in expected["kpis_2025"].items():
        assert kpis.ltm[name].values["2025-12"] == Decimal(value), name
    assert kpis.ltm["nettoomsattning"].values["2025-11"] is None
    assert kpis.ltm["bruttoresultat"].values["2025-12"] == Decimal("100000.00")
    assert kpis.ltm["bruttomarginal"].values["2025-12"] == 1
    assert kpis.ltm["ebitda_marginal"].values["2025-12"] == Decimal("0.1")
    assert kpis.ltm["ebit"].values["2025-12"] == Decimal("10000.00")
    assert kpis.ltm["tillvaxt"].values["2025-12"] is None
    assert kpis.monthly["nettoomsattning"].values["2025-01"] == Decimal("100000.00")
    assert kpis.monthly["ebitda_marginal"].values["2025-02"] is None  # no revenue
    assert "3000–3699" in kpis.monthly["nettoomsattning"].source_ref

    d = _kpis(demo).ltm
    assert d["nettoomsattning"].values["2024-12"] == Decimal("38500000.00")
    assert d["nettoomsattning"].values["2025-12"] == Decimal("42100000.00")
    assert d["tillvaxt"].values["2025-12"] == Decimal("42100000") / Decimal("38500000") - 1
    assert d["tillvaxt"].values["2025-11"] is None
    for month in ("2024-12", "2025-12"):
        v = {k: s.values[month] for k, s in d.items()}
        assert v["ebitda"] == (
            v["bruttoresultat"]
            + v["ovriga_rorelseintakter"]
            - v["ovriga_externa_kostnader"]
            - v["personalkostnader"]
            - v["ovriga_rorelsekostnader"]
        )
        assert v["ebit"] == v["ebitda"] - v["avskrivningar"]
        assert v["avskrivningar"] == Decimal("300000.00")


def _with_vouchers(ledger: Ledger, *lines: tuple[str, str, str]) -> Ledger:
    """Add one balanced voucher per (debit account, credit account, amount) in April."""
    extra = [
        Voucher(
            series="X",
            number=str(i),
            date=dt.date(2025, 4, 10),
            transactions=[
                Transaction(account=debit, amount=Decimal(amount)),
                Transaction(account=credit, amount=-Decimal(amount)),
            ],
        )
        for i, (debit, credit, amount) in enumerate(lines, start=1)
    ]
    return ledger.model_copy(update={"vouchers": [*ledger.vouchers, *extra]})


def test_nwc_monthly_and_normal_level(minimal: Ledger) -> None:
    kpis = _kpis(minimal)

    nwc = kpis.nwc.values
    assert (nwc["2025-01"], nwc["2025-02"]) == (Decimal("-25000.00"), Decimal("-45000.00"))
    assert all(nwc[f"2025-{m:02d}"] == Decimal("-35000.00") for m in range(3, 13))
    assert kpis.nwc_normal.value == Decimal("-35000.00")
    assert "12" in kpis.nwc_normal.source_ref

    # Bank overdraft (2417), short term loan (2840), owner loan (2893) and an unpaid
    # dividend (2898) are financing, not working capital.
    financed = _with_vouchers(
        minimal,
        ("1930", "2417", "50000"),
        ("1930", "2840", "30000"),
        ("1930", "2893", "20000"),
        ("2091", "2898", "15000"),
    )
    assert _kpis(financed).nwc.values == kpis.nwc.values


def test_net_debt_items(minimal: Ledger, demo: Ledger) -> None:
    kpis = _kpis(minimal)

    assert kpis.net_debt_month == "2025-12"
    items = {i.kind: i for i in kpis.net_debt_items}
    assert items["kassa"].amount == Decimal("-145000.00") and items["kassa"].accounts == ["1930"]
    # Open year: 20.6 % of the 10 000 profit so far, not yet booked.
    assert items["beraknad_skatt"].amount == Decimal("2060.00")
    assert kpis.net_debt == Decimal("-142940.00")
    assert kpis.net_debt == sum(i.amount for i in kpis.net_debt_items)
    assert all(i.source_ref for i in kpis.net_debt_items)

    financed = _kpis(
        _with_vouchers(
            minimal,
            ("1930", "2417", "50000"),
            ("1930", "2840", "30000"),
            ("1930", "2893", "20000"),
        )
    )
    interest = next(i for i in financed.net_debt_items if i.kind == "rantebarande")
    assert interest.accounts == ["2417", "2840"] and interest.amount == Decimal("80000.00")
    assert [(i.accounts, i.amount) for i in financed.debt_like_items] == [
        (["2893"], Decimal("20000.00"))
    ]
    # Owner loan is listed for assessment, not added to net debt; the cash it brought is.
    assert financed.net_debt == kpis.net_debt + Decimal("80000") - Decimal("100000")

    d = _kpis(demo)
    demo_items = {i.kind: i for i in d.net_debt_items}
    assert demo_items["rantebarande"].accounts == ["2350"]
    assert demo_items["rantebarande"].amount == Decimal("1800000.00")
    ebt_2025 = -sum(
        t.amount
        for v in demo.vouchers
        if v.date.year == 2025
        for t in v.transactions
        if 3000 <= int(t.account) <= 8899
    )
    assert demo_items["beraknad_skatt"].amount == (ebt_2025 * Decimal("0.206")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

"""M2 analysis tests. One test per acceptance criterion in docs/PLAN.md."""

from decimal import Decimal
from pathlib import Path

import pytest

from diligens.analysis.mapping import map_accounts
from diligens.analysis.statements import monthly_statements
from diligens.models import Account, Ledger, StatementLine
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

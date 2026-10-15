"""M2 analysis tests. One test per acceptance criterion in docs/PLAN.md."""

import ast
import datetime as dt
import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

from diligens import analysis
from diligens.analysis.kpis import compute_kpis
from diligens.analysis.mapping import map_accounts
from diligens.analysis.reconcile import reconcile
from diligens.analysis.statements import monthly_statements
from diligens.models import (
    Account,
    Balance,
    FiscalYear,
    KPIs,
    Ledger,
    NetDebtItem,
    Reconciliation,
    StatementLine,
    Transaction,
    Voucher,
)
from diligens.sie.generator import build_demo_company, generate_demo_company
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


def _voucher(ref: int, date: dt.date, debit: str, credit: str, amount: str) -> Voucher:
    return Voucher(
        series="X",
        number=str(ref),
        date=date,
        transactions=[
            Transaction(account=debit, amount=Decimal(amount)),
            Transaction(account=credit, amount=-Decimal(amount)),
        ],
    )


def _with_vouchers(ledger: Ledger, *lines: tuple[str, str, str]) -> Ledger:
    """Add one balanced voucher per (debit account, credit account, amount) in April 2025."""
    extra = [
        _voucher(i, dt.date(2025, 4, 10), debit, credit, amount)
        for i, (debit, credit, amount) in enumerate(lines, start=1)
    ]
    return ledger.model_copy(update={"vouchers": [*ledger.vouchers, *extra]})


def _ledger(years: list[FiscalYear], vouchers: list[Voucher]) -> Ledger:
    return Ledger(
        company_name="Test AB",
        org_nr="999999-9999",
        fiscal_years=years,
        accounts={},
        opening_balances=[],
        closing_balances=[],
        results=[],
        vouchers=vouchers,
    )


def _calendar_year(index: int, year: int) -> FiscalYear:
    return FiscalYear(index=index, start=dt.date(year, 1, 1), end=dt.date(year, 12, 31))


# Fiscal year starting mid month and not following the calendar, e.g. a new company.
BROKEN_YEAR = FiscalYear(index=0, start=dt.date(2024, 5, 15), end=dt.date(2025, 4, 30))
BROKEN_YEAR_LEDGER = _ledger(
    [BROKEN_YEAR],
    [
        _voucher(1, dt.date(2024, 5, 20), "1930", "3041", "100000"),
        _voucher(2, dt.date(2025, 4, 10), "5010", "1930", "40000"),
    ],
)


def test_monthly_statements(minimal: Ledger, demo: Ledger) -> None:
    st = monthly_statements(minimal, map_accounts(minimal))

    # Stops at the last month with vouchers, like an export taken mid year.
    assert st.months == ["2025-01", "2025-02", "2025-03"]
    revenue = _line(st.income, "Nettoomsättning")
    assert revenue.values["2025-01"] == Decimal("-100000.00")
    assert revenue.values["2025-02"] == 0
    assert revenue.accounts == ["3041"] and "3000–3699" in revenue.source_ref
    external = _line(st.income, "Övriga externa kostnader")
    assert (external.values["2025-02"], external.values["2025-03"]) == (20000, 40000)
    assert _line(st.income, "Personalkostnader").values["2025-03"] == 30000
    assert st.account_movements["3041"]["2025-01"] == Decimal("-100000.00")

    cash = _line(st.balance, "Kassa och bank")
    assert [cash.values[m] for m in ("2025-01", "2025-03")] == [225000, 145000]
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

    cut = demo.model_copy(
        update={"vouchers": [v for v in demo.vouchers if v.date <= dt.date(2025, 9, 30)]}
    )
    assert monthly_statements(cut, map_accounts(cut)).months[-1] == "2025-09"

    # A middle year without vouchers stays in, so months remain contiguous.
    gap = _ledger(
        [_calendar_year(-2, 2023), _calendar_year(-1, 2024), _calendar_year(0, 2025)],
        [
            _voucher(1, dt.date(2023, 1, 10), "1930", "3041", "100"),
            _voucher(2, dt.date(2025, 1, 10), "1930", "3041", "100"),
        ],
    )
    gap_months = monthly_statements(gap, map_accounts(gap)).months
    assert len(gap_months) == 25 and gap_months[12] == "2024-01"

    broken = monthly_statements(BROKEN_YEAR_LEDGER, map_accounts(BROKEN_YEAR_LEDGER))
    assert broken.months[0] == "2024-05" and broken.months[-1] == "2025-04"
    assert len(broken.months) == 12


def _kpis(ledger: Ledger) -> KPIs:
    return compute_kpis(monthly_statements(ledger, map_accounts(ledger)))


def test_kpis(minimal: Ledger, demo: Ledger) -> None:
    expected = json.loads((FIXTURES / "minimal_expected.json").read_text(encoding="utf-8"))
    kpis = _kpis(minimal)

    for name, value in expected["kpis_2025"].items():
        assert kpis.ytd[name].values["2025-03"] == Decimal(value), name
    assert kpis.ytd["bruttoresultat"].values["2025-03"] == Decimal("100000.00")
    assert kpis.ytd["bruttomarginal"].values["2025-03"] == 1
    assert kpis.ytd["ebitda_marginal"].values["2025-03"] == Decimal("0.1")
    assert kpis.ytd["ebit"].values["2025-03"] == Decimal("10000.00")
    assert kpis.ltm["nettoomsattning"].values["2025-03"] is None  # only 3 months
    assert kpis.ltm["tillvaxt"].values["2025-03"] is None
    assert kpis.monthly["nettoomsattning"].values["2025-01"] == Decimal("100000.00")
    assert kpis.monthly["ebitda_marginal"].values["2025-02"] is None  # no revenue
    assert "3000–3699" in kpis.monthly["nettoomsattning"].source_ref

    demo_kpis = _kpis(demo)
    d = demo_kpis.ltm
    assert d["nettoomsattning"].values["2024-12"] == Decimal("38500000.00")
    assert d["nettoomsattning"].values["2025-12"] == Decimal("42100000.00")
    assert d["tillvaxt"].values["2025-12"] == Decimal("42100000") / Decimal("38500000") - 1
    assert d["tillvaxt"].values["2025-11"] is None
    assert demo_kpis.ytd["ebitda"].values["2025-12"] == d["ebitda"].values["2025-12"]
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

    with pytest.raises(ValueError, match="Inga verifikationer"):
        _kpis(minimal.model_copy(update={"vouchers": []}))


def test_nwc_monthly_and_normal_level(minimal: Ledger, demo: Ledger) -> None:
    kpis = _kpis(minimal)

    nwc = kpis.nwc.values
    assert [nwc[m] for m in kpis.months] == [
        Decimal("-25000.00"),
        Decimal("-45000.00"),
        Decimal("-35000.00"),
    ]
    assert kpis.nwc_normal.value is None  # needs 12 month ends

    demo_kpis = _kpis(demo)
    year_end = {b.account: b.amount for b in demo.closing_balances if b.year_index == 0}
    excluded = {"2410", "2417", "2840", "2893", "2898"}
    in_nwc = [
        a
        for a in year_end
        if (1400 <= int(a) <= 1799 or 2400 <= int(a) <= 2499 or 2600 <= int(a) <= 2999)
        and a not in excluded
        and not 2860 <= int(a) <= 2869
    ]
    assert demo_kpis.nwc.values["2025-12"] == sum(year_end[a] for a in in_nwc)
    last_12 = [demo_kpis.nwc.values[f"2025-{m:02d}"] or 0 for m in range(1, 13)]
    assert demo_kpis.nwc_normal.value == sum(last_12) / 12
    assert "12" in demo_kpis.nwc_normal.source_ref

    # Financing on both sides is not working capital: bank overdraft (2417), short term
    # loan (2840), owner loan (2893), unpaid dividend (2898), tax account (1630), group
    # receivable (1661), loan receivable (1682) and a loan to the owner (1685).
    financed = _kpis(
        _with_vouchers(
            minimal,
            ("1930", "2417", "50000"),
            ("1930", "2840", "30000"),
            ("1930", "2893", "20000"),
            ("2091", "2898", "15000"),
            ("1630", "1930", "12000"),
            ("1661", "1930", "9000"),
            ("1682", "1930", "7000"),
            ("1685", "1930", "40000"),
        )
    )
    assert [financed.nwc.values[m] for m in kpis.months] == [nwc[m] for m in kpis.months]
    assert financed.nwc.values["2025-04"] == nwc["2025-03"]


def _estimated(kpis: KPIs) -> list[NetDebtItem]:
    return [i for i in kpis.net_debt_items if i.kind == "beraknad_skatt"]


def test_net_debt_items(minimal: Ledger, demo: Ledger) -> None:
    kpis = _kpis(minimal)

    assert kpis.net_debt_month == "2025-03"
    items = {i.kind: i for i in kpis.net_debt_items}
    assert items["kassa"].amount == Decimal("-145000.00") and items["kassa"].accounts == ["1930"]
    # Open year: 20.6 % of the 10 000 profit so far, not yet booked.
    assert items["beraknad_skatt"].amount == Decimal("2060.00")
    assert "2025" in items["beraknad_skatt"].label
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

    # Receivables mirror the debts: the tax account sits with the tax debts in net debt,
    # group, loan and owner receivables are listed for assessment.
    lent = _kpis(
        _with_vouchers(
            minimal,
            ("1630", "1930", "12000"),
            ("1661", "1930", "9000"),
            ("1682", "1930", "7000"),
            ("1685", "1930", "40000"),
        )
    )
    tax = next(i for i in lent.net_debt_items if i.kind == "skatt")
    assert tax.accounts == ["1630"] and tax.amount == Decimal("-12000.00")
    assert [(i.accounts, i.amount) for i in lent.debt_like_items] == [
        (["1661"], Decimal("-9000.00")),
        (["1682", "1685"], Decimal("-47000.00")),
    ]
    # Only the cash leaving (68 000) and the tax account (-12 000) move net debt.
    assert lent.net_debt == kpis.net_debt + Decimal("68000") - Decimal("12000")

    # Tax already booked on 8910 means the company has taken a view: no estimate.
    taxed = _kpis(_with_vouchers(minimal, ("8910", "2512", "1000")))
    assert _estimated(taxed) == []

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
    tax_2025 = (ebt_2025 * Decimal("0.206")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert [i.amount for i in _estimated(d)] == [tax_2025]  # 2024 is closed

    # Deal in early 2026 before 2025 is closed: both open years get a line.
    early_2026 = demo.model_copy(
        update={
            "fiscal_years": [*demo.fiscal_years, _calendar_year(1, 2026)],
            "vouchers": [
                *demo.vouchers,
                _voucher(1, dt.date(2026, 1, 15), "1930", "3041", "1000"),
            ],
        }
    )
    both = _kpis(early_2026)
    assert both.net_debt_month == "2026-01"
    assert [(i.label, i.amount) for i in _estimated(both)] == [
        ("Beräknad skatt 2025 (ej bokförd)", tax_2025),
        ("Beräknad skatt 2026 (ej bokförd)", Decimal("206.00")),
    ]

    # Broken fiscal year starting mid month: May revenue counts. 20.6 % of 60 000.
    broken = _kpis(BROKEN_YEAR_LEDGER)
    assert [(i.label, i.amount) for i in _estimated(broken)] == [
        ("Beräknad skatt 2024/25 (ej bokförd)", Decimal("12360.00"))
    ]


def _reconcile(ledger: Ledger) -> Reconciliation:
    return reconcile(ledger, monthly_statements(ledger, map_accounts(ledger)))


def test_reconciliation_demo_is_zero(minimal: Ledger, tmp_path: Path) -> None:
    generate_demo_company(42, tmp_path / "demo_bolag.se")
    demo = parse_sie(tmp_path / "demo_bolag.se")

    for ledger in (minimal, demo):
        rec = _reconcile(ledger)
        assert rec.ok and rec.total_difference == 0
    rec = _reconcile(demo)
    assert {(r.year_index, r.against) for r in rec.rows} == {
        (-1, "RES"),
        (-1, "UB"),
        (0, "RES"),
        (0, "UB"),
    }
    assert any(r.account == "3041" and r.against == "RES" for r in rec.rows)
    dumped = rec.model_dump()
    assert dumped["ok"] is True and dumped["rows"][0]["difference"] == 0

    def bump(balances: list[Balance], account: str) -> list[Balance]:
        return [
            b.model_copy(update={"amount": b.amount + 1})
            if (b.year_index, b.account) == (0, account)
            else b
            for b in balances
        ]

    tampered = demo.model_copy(
        update={
            "closing_balances": bump(demo.closing_balances, "1930"),
            "results": bump(demo.results, "6540"),
        }
    )
    off = [(r.against, r.account, r.difference) for r in _reconcile(tampered).rows if r.difference]
    assert sorted(off) == [("RES", "6540", Decimal("-1")), ("UB", "1930", Decimal("-1"))]

    # A #RES row on a balance sheet account is not silently skipped.
    misplaced = minimal.model_copy(
        update={"results": [*minimal.results, Balance(year_index=0, account="1930", amount=5)]}
    )
    assert [
        (r.against, r.account, r.difference) for r in _reconcile(misplaced).rows if r.difference
    ] == [("RES", "1930", Decimal("-5"))]

    stray = _with_vouchers(minimal, ("1930", "9999", "100"))
    rec = _reconcile(stray)
    assert rec.unmapped_accounts == ["9999"] and not rec.ok

    early = minimal.model_copy(
        update={
            "vouchers": [
                *minimal.vouchers,
                _voucher(9, dt.date(2024, 12, 31), "5010", "1930", "50000"),
            ]
        }
    )
    rec = _reconcile(early)
    assert rec.vouchers_outside_years == ["X9"] and not rec.ok

    assert not _reconcile(minimal.model_copy(update={"vouchers": []})).ok


FORBIDDEN_MODULES = {
    "anthropic",
    "httpx",
    "io",
    "openpyxl",
    "os",
    "pathlib",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "urllib",
}
FORBIDDEN_CALLS = {"open", "print", "input", "eval", "exec"}


def test_analysis_is_pure(minimal: Ledger) -> None:
    package = Path(analysis.__file__).parent
    for source in sorted(package.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                names = []
            for name in names:
                assert name.split(".")[0] not in FORBIDDEN_MODULES, (source.name, name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, (source.name, node.func.id)

    before = minimal.model_dump()
    mapping = map_accounts(minimal)
    statements = monthly_statements(minimal, mapping)
    first = (compute_kpis(statements), reconcile(minimal, statements))
    second = (compute_kpis(statements), reconcile(minimal, statements))
    assert minimal.model_dump() == before
    assert first == second

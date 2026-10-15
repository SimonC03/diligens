"""Shared Pydantic models. The only types that cross module boundaries.

Money is Decimal with raw SIE signs (debit positive, credit negative) unless a field name
says otherwise.
"""

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, computed_field

# ---------------------------------------------------------------- ledger (sie/)


AccountType = Literal["T", "S", "K", "I"]


class Account(BaseModel):
    number: str
    name: str
    type: AccountType | None = Field(default=None, description="#KTYP, optional")


class FiscalYear(BaseModel):
    index: int = Field(description="0 = current, -1 = previous, as in #RAR")
    start: dt.date
    end: dt.date


class Balance(BaseModel):
    year_index: int
    account: str
    amount: Decimal


class PeriodBalance(BaseModel):
    year_index: int
    period: str = Field(description="YYYYMM as in #PSALDO")
    account: str
    amount: Decimal
    objects: list[tuple[str, str]] = Field(
        default_factory=list, description="empty for the account total, else one dimension"
    )


class Transaction(BaseModel):
    account: str
    amount: Decimal
    date: dt.date | None = None
    text: str | None = None
    objects: list[tuple[str, str]] = Field(
        default_factory=list, description="(dimension, object) pairs from the {} list"
    )


class Voucher(BaseModel):
    series: str
    number: str
    date: dt.date
    text: str = ""
    reg_date: dt.date | None = None
    transactions: list[Transaction]

    @property
    def ref(self) -> str:
        """Stable source reference, e.g. 'B2'."""
        return f"{self.series}{self.number}"


class Ledger(BaseModel):
    company_name: str
    org_nr: str
    program: str | None = Field(default=None, description="#PROGRAM")
    generated: dt.date | None = Field(default=None, description="#GEN")
    chart_type: str | None = Field(default=None, description="#KPTYP")
    fiscal_years: list[FiscalYear]
    accounts: dict[str, Account]
    opening_balances: list[Balance] = Field(description="#IB")
    closing_balances: list[Balance] = Field(description="#UB")
    results: list[Balance] = Field(description="#RES")
    period_balances: list[PeriodBalance] = Field(default_factory=list, description="#PSALDO")
    vouchers: list[Voucher]
    warnings: list[str] = Field(default_factory=list, description="unknown tags etc.")


class IntegrityIssue(BaseModel):
    kind: Literal[
        "unbalanced_voucher",
        "ib_trans_ub_mismatch",
        "trans_res_mismatch",
        "ub_res_mismatch",
        "voucher_outside_fiscal_year",
        "unknown_account_class",
    ]
    ref: str = Field(description="voucher ref, account number or 'RAR <index>'")
    expected: Decimal
    actual: Decimal


# ---------------------------------------------------------------- analysis (analysis/)

StatementKind = Literal["RR", "BR"]


class MappedAccount(BaseModel):
    account: str
    line: str = Field(description="Swedish line name from DOMAIN.md section 2")
    statement: StatementKind


class Mapping(BaseModel):
    accounts: dict[str, MappedAccount]
    unmapped: list[str] = Field(description="accounts outside every BAS range, sorted")


class StatementLine(BaseModel):
    line: str
    statement: StatementKind
    accounts: list[str]
    values: dict[str, Decimal] = Field(
        description="month 'YYYY-MM' to raw amount: RR movement in the month, BR month end"
    )
    source_ref: str


class Statements(BaseModel):
    """Monthly statements in raw SIE signs. Signs are flipped only at export."""

    fiscal_years: list[FiscalYear] = Field(description="years with vouchers, oldest first")
    months: list[str] = Field(description="'YYYY-MM' for every month of those years")
    income: list[StatementLine]
    balance: list[StatementLine] = Field(
        description="BR lines plus 'Årets resultat (beräknat)' so every month sums to zero"
    )
    account_movements: dict[str, dict[str, Decimal]] = Field(
        description="RR account to month to movement"
    )
    account_balances: dict[str, dict[str, Decimal]] = Field(
        description="BR account to month to month end balance"
    )


class Figure(BaseModel):
    value: Decimal | None = Field(description="None when the data does not allow it")
    source_ref: str


class KpiSeries(BaseModel):
    name: str = Field(description="Swedish label")
    values: dict[str, Decimal | None] = Field(description="month 'YYYY-MM' to value")
    source_ref: str


NetDebtKind = Literal[
    "rantebarande", "skatt", "beraknad_skatt", "kassa", "placeringar", "att_bedoma"
]


class NetDebtItem(BaseModel):
    label: str = Field(description="Swedish label")
    kind: NetDebtKind
    accounts: list[str]
    amount: Decimal = Field(description="positive increases net debt")
    source_ref: str


class KPIs(BaseModel):
    """Values follow DOMAIN.md section 4 and 5: revenue, costs and EBITDA are positive."""

    months: list[str]
    monthly: dict[str, KpiSeries] = Field(description="amounts and margins per month")
    ltm: dict[str, KpiSeries] = Field(description="rolling 12 months, None before month 12")
    ytd: dict[str, KpiSeries] = Field(description="cumulative within each fiscal year")
    nwc: KpiSeries = Field(description="month end NWC, assets minus liabilities")
    nwc_normal: Figure = Field(description="average of the last 12 month end values")
    net_debt_month: str
    net_debt_items: list[NetDebtItem]
    net_debt: Decimal
    debt_like_items: list[NetDebtItem] = Field(
        description="excluded from NWC and from net debt; for the advisor to assess"
    )


class ReconciliationRow(BaseModel):
    year_index: int
    account: str
    against: Literal["RES", "UB"]
    computed: Decimal = Field(description="from the monthly statements")
    stated: Decimal = Field(description="from the SIE file, 0.00 if missing")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def difference(self) -> Decimal:
        return self.computed - self.stated


class Reconciliation(BaseModel):
    rows: list[ReconciliationRow]
    unmapped_accounts: list[str]
    vouchers_outside_years: list[str] = Field(
        default_factory=list, description="voucher refs dated outside every fiscal year"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_difference(self) -> Decimal:
        return sum((abs(r.difference) for r in self.rows), Decimal("0"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        """True only when there is something to reconcile and nothing is off or left out."""
        return (
            bool(self.rows)
            and self.total_difference == 0
            and not self.unmapped_accounts
            and not self.vouchers_outside_years
        )


# ---------------------------------------------------------------- flags (agent/)


class PrefilterHit(BaseModel):
    """A voucher the deterministic prefilter sends on for classification."""

    voucher_ref: str
    accounts: list[str] = Field(description="EBITDA accounts on the voucher that triggered")
    reasons: list[str] = Field(description="Swedish, one per rule that matched")


FlagCategory = Literal[
    "engangspost", "agarrelaterad", "narstaende", "periodisering", "avvikelse", "normal"
]


class Flag(BaseModel):
    voucher_ref: str
    account: str
    amount: Decimal
    category: FlagCategory
    rationale: str = Field(description="Swedish, one or two sentences")
    confidence: float = Field(ge=0.0, le=1.0)
    ebitda_adjustment: Decimal = Field(
        default=Decimal("0"), description="positive adds back to EBITDA, computed in code"
    )

    @property
    def needs_review(self) -> bool:
        return self.confidence < 0.7


class DDQuestion(BaseModel):
    text: str = Field(description="Swedish question to the seller")
    voucher_refs: list[str] = Field(default_factory=list)
    accounts: list[str] = Field(default_factory=list)
    amount: Decimal | None = None


# ---------------------------------------------------------------- facit (sie/generator)


class PlantedAnomaly(BaseModel):
    id: int
    category: FlagCategory
    also_accepted: list[FlagCategory] = Field(
        default_factory=list, description="other categories an evaluator should accept"
    )
    description: str
    voucher_refs: list[str]
    accounts: list[str]
    total_amount: Decimal


class Facit(BaseModel):
    seed: int
    anomalies: list[PlantedAnomaly]

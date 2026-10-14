"""Shared Pydantic models. The only types that cross module boundaries.

Money is Decimal with raw SIE signs (debit positive, credit negative) unless a field name
says otherwise.
"""

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

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
    kind: Literal["unbalanced_voucher", "ib_trans_ub_mismatch", "trans_res_mismatch"]
    ref: str = Field(description="voucher ref or account number")
    expected: Decimal
    actual: Decimal


# ---------------------------------------------------------------- flags (agent/)

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

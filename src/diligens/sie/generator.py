"""Synthetic demo company: a fictional Swedish consultancy, two fiscal years 2024 and 2025.

All amounts are built deterministically from `random.Random(seed)`. The ledger is assembled
from voucher drafts, so IB, UB and RES follow from the transactions and the integrity rules
in docs/DOMAIN.md section 1 hold by construction. Planted anomalies (section 6) are listed
in the returned Facit.
"""

import calendar
import datetime as dt
import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from diligens.models import (
    Account,
    AccountType,
    Balance,
    Facit,
    FiscalYear,
    FlagCategory,
    Ledger,
    PlantedAnomaly,
    Transaction,
    Voucher,
)
from diligens.sie.writer import write_sie

COMPANY_NAME = "Demobolaget Konsult AB"
ORG_NR = "999999-9999"
PROGRAM = "Diligens demogenerator"
GENERATED = dt.date(2026, 1, 15)
YEARS = (2024, 2025)
FIRST_DAY = dt.date(2024, 1, 1)
LAST_DAY = dt.date(2025, 12, 31)

# Series as in common Swedish accounting programs.
MANUAL, SALES, RECEIPTS, PURCHASES, PAYMENTS, PAYROLL = "A", "B", "C", "D", "E", "L"

VAT = Decimal("0.25")
ZERO = Decimal("0")
CENT = Decimal("0.01")

ACCOUNTS: dict[str, str] = {
    "1220": "Inventarier och verktyg",
    "1229": "Ackumulerade avskrivningar på inventarier och verktyg",
    "1510": "Kundfordringar",
    "1930": "Företagskonto",
    "2081": "Aktiekapital",
    "2091": "Balanserad vinst eller förlust",
    "2099": "Årets resultat",
    "2350": "Andra långfristiga skulder till kreditinstitut",
    "2440": "Leverantörsskulder",
    "2512": "Beräknad inkomstskatt",
    "2611": "Utgående moms på försäljning inom Sverige, 25 %",
    "2641": "Debiterad ingående moms",
    "2650": "Redovisningskonto för moms",
    "2710": "Personalskatt",
    "2731": "Avräkning lagstadgade sociala avgifter",
    "3041": "Försäljning tjänster 25 % sv",
    "3990": "Övriga ersättningar och intäkter",
    "4600": "Legoarbeten och underentreprenader",
    "5010": "Lokalhyra",
    "5410": "Förbrukningsinventarier",
    "5615": "Leasing av personbilar",
    "5800": "Resekostnader",
    "5910": "Annonsering",
    "6110": "Kontorsmateriel",
    "6212": "Mobiltelefon",
    "6310": "Företagsförsäkringar",
    "6530": "Redovisningstjänster",
    "6540": "IT-tjänster",
    "6550": "Konsultarvoden",
    "6570": "Bankkostnader",
    "6991": "Övriga externa kostnader, avdragsgilla",
    "7210": "Löner till tjänstemän",
    "7411": "Pensionsförsäkringspremier",
    "7510": "Arbetsgivaravgifter 31,42 %",
    "7610": "Utbildning",
    "7832": "Avskrivningar på inventarier och verktyg",
    "8410": "Räntekostnader för långfristiga skulder",
    "8910": "Skatt som belastar årets resultat",
    "8999": "Årets resultat",
}

# Net revenue per year and monthly profile (sums to 12): July holidays, strong autumn.
REVENUE = {2024: Decimal("38500000"), 2025: Decimal("41500000")}
SEASON = (0.95, 1.0, 1.1, 1.0, 1.05, 1.0, 0.45, 0.85, 1.15, 1.15, 1.15, 1.15)

# Recurring supplier costs: account, monthly net amount, invoices per month, VAT rate.
SUPPLIER_COSTS: tuple[tuple[str, int, int, Decimal, str], ...] = (
    ("5410", 22000, 1, VAT, "Kontorsutrustning"),
    ("5800", 38000, 2, Decimal("0.12"), "Resor och hotell"),
    ("5910", 20000, 1, VAT, "Annonsering"),
    ("6110", 7000, 1, VAT, "Kontorsmaterial"),
    ("6212", 16000, 1, VAT, "Mobil och data"),
    ("6310", 9000, 1, ZERO, "Företagsförsäkring"),
    ("6530", 18000, 1, VAT, "Redovisning och lön"),
    ("6540", 56000, 2, VAT, "IT-drift och licenser"),
    ("6991", 5000, 1, VAT, "Övriga kostnader"),
    ("7610", 28000, 1, VAT, "Kurs och utbildning"),
)

SOCIAL_FEES = Decimal("0.3142")
PENSION = Decimal("0.045")
WITHHOLDING = Decimal("0.30")
CORPORATE_TAX = Decimal("0.206")


@dataclass
class _Draft:
    series: str
    date: dt.date
    text: str
    lines: list[tuple[str, Decimal]]
    anomaly: int | None = None
    seq: int = 0


@dataclass
class _Book:
    rng: random.Random
    drafts: list[_Draft] = field(default_factory=list)

    def add(
        self,
        series: str,
        date: dt.date,
        text: str,
        lines: list[tuple[str, Decimal]],
        anomaly: int | None = None,
    ) -> _Draft:
        if sum((amount for _, amount in lines), ZERO) != ZERO:
            raise ValueError(f"unbalanced draft {text!r}")
        draft = _Draft(series, date, text, lines, anomaly, seq=len(self.drafts))
        self.drafts.append(draft)
        return draft

    def supplier_invoice(
        self,
        date: dt.date,
        text: str,
        account: str,
        net: Decimal,
        vat_rate: Decimal = VAT,
        anomaly: int | None = None,
        payable_days: int = 30,
    ) -> _Draft:
        vat = _round(net * vat_rate)
        lines = [(account, net), ("2440", -(net + vat))]
        if vat:
            lines.insert(1, ("2641", vat))
        draft = self.add(PURCHASES, date, text, lines, anomaly)
        pay_date = date + dt.timedelta(days=payable_days + self.rng.randint(-3, 3))
        if pay_date <= LAST_DAY:
            self.add(
                PAYMENTS,
                pay_date,
                f"Betalning {text}",
                [("2440", net + vat), ("1930", -(net + vat))],
            )
        return draft

    def customer_invoice(
        self,
        date: dt.date,
        text: str,
        net: Decimal,
        anomaly: int | None = None,
        paid: bool = True,
    ) -> _Draft:
        vat = _round(net * VAT)
        draft = self.add(
            SALES, date, text, [("1510", net + vat), ("3041", -net), ("2611", -vat)], anomaly
        )
        pay_date = date + dt.timedelta(days=30 + self.rng.randint(-4, 12))
        if paid and pay_date <= LAST_DAY:
            self.add(
                RECEIPTS,
                pay_date,
                f"Inbetalning {text}",
                [("1930", net + vat), ("1510", -(net + vat))],
            )
        return draft


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _kr(value: float) -> Decimal:
    """Whole kronor as Decimal with two decimals."""
    return Decimal(round(value)).quantize(CENT)


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _next_month(year: int, month: int, day: int) -> dt.date:
    return dt.date(year + month // 12, month % 12 + 1, day)


def _months() -> list[tuple[int, int]]:
    return [(y, m) for y in YEARS for m in range(1, 13)]


OPENING_2024: dict[str, Decimal] = {
    "1220": Decimal("1200000.00"),
    "1229": Decimal("-300000.00"),
    "1510": Decimal("4250000.00"),
    "1930": Decimal("3100000.00"),
    "2081": Decimal("-100000.00"),
    "2350": Decimal("-3000000.00"),
    "2440": Decimal("-875000.00"),
}


def _opening_settlements(book: _Book) -> None:
    """Pay the 2023 receivables and payables in the opening balance during January 2024."""
    receivables = [
        Decimal("1350000.00"),
        Decimal("1100000.00"),
        Decimal("950000.00"),
        Decimal("850000.00"),
    ]
    for i, amount in enumerate(receivables, start=1):
        book.add(
            RECEIPTS,
            dt.date(2024, 1, 5 + 5 * i),
            f"Inbetalning kundfaktura 2023, kund {1000 + i}",
            [("1930", amount), ("1510", -amount)],
        )
    payables = [Decimal("400000.00"), Decimal("275000.00"), Decimal("200000.00")]
    for i, amount in enumerate(payables, start=1):
        book.add(
            PAYMENTS,
            dt.date(2024, 1, 8 + 6 * i),
            f"Betalning leverantörsfaktura 2023, leverantör {i}",
            [("2440", amount), ("1930", -amount)],
        )


def _sales(book: _Book) -> None:
    rng = book.rng
    invoice_no = 24000
    for year, month in _months():
        target = REVENUE[year] * Decimal(str(SEASON[month - 1])) / 12
        count = rng.randint(19, 21)
        weights = [rng.uniform(0.4, 1.8) for _ in range(count)]
        total_weight = sum(weights)
        amounts = [_kr(float(target) * w / total_weight) for w in weights]
        amounts[-1] += _kr(float(target)) - sum(amounts, ZERO)
        for amount in amounts:
            invoice_no += 1
            customer = rng.randint(1001, 1060)
            day = rng.randint(1, 28)
            book.customer_invoice(
                dt.date(year, month, day), f"Faktura {invoice_no} kund {customer}", amount
            )


def _purchases(book: _Book) -> None:
    rng = book.rng
    for year, month in _months():
        rent = Decimal("145000.00")
        book.supplier_invoice(
            dt.date(year, month, 1), f"Hyra kontor {year}-{month:02d}", "5010", rent
        )

        sub_total = float(REVENUE[year]) / 12 * SEASON[month - 1] * 0.13
        weights = [rng.uniform(0.6, 1.4) for _ in range(3)]
        for weight in weights:
            book.supplier_invoice(
                dt.date(year, month, rng.randint(3, 27)),
                f"Underkonsult uppdrag {rng.randint(100, 199)}",
                "4600",
                _kr(sub_total * weight / sum(weights)),
            )

        for account, monthly, count, vat_rate, label in SUPPLIER_COSTS:
            for _ in range(count):
                net = _kr(monthly / count * rng.uniform(0.75, 1.25))
                book.supplier_invoice(
                    dt.date(year, month, rng.randint(2, 27)), label, account, net, vat_rate
                )


def _payroll(book: _Book) -> None:
    rng = book.rng
    headcount = {2024: 30, 2025: 32}
    avg_salary = {2024: 47000, 2025: 48500}
    for year, month in _months():
        gross = _kr(headcount[year] * avg_salary[year] * rng.uniform(0.98, 1.02))
        tax = _round(gross * WITHHOLDING)
        fees = _round(gross * SOCIAL_FEES)
        pension = _round(gross * PENSION)
        payday = dt.date(year, month, 25)
        book.add(
            PAYROLL,
            payday,
            f"Löner {year}-{month:02d}",
            [
                ("7210", gross),
                ("7510", fees),
                ("7411", pension),
                ("2710", -tax),
                ("2731", -fees),
                ("1930", -(gross - tax + pension)),
            ],
        )
        due = _next_month(year, month, 12)
        if due <= LAST_DAY:
            book.add(
                MANUAL,
                due,
                f"Skattedeklaration löner {year}-{month:02d}",
                [("2710", tax), ("2731", fees), ("1930", -(tax + fees))],
            )


def _financing_and_depreciation(book: _Book) -> None:
    rng = book.rng
    loan = -OPENING_2024["2350"]
    amortisation = Decimal("50000.00")
    for year, month in _months():
        interest = _round(loan * Decimal("0.055") / 12)
        book.add(
            MANUAL,
            dt.date(year, month, 25),
            f"Ränta och amortering banklån {year}-{month:02d}",
            [("8410", interest), ("2350", amortisation), ("1930", -(interest + amortisation))],
        )
        loan -= amortisation
        end = _month_end(year, month)
        depreciation = Decimal("25000.00")
        book.add(
            MANUAL,
            end,
            f"Avskrivning inventarier {year}-{month:02d}",
            [("7832", depreciation), ("1229", -depreciation)],
        )
        fee = _kr(rng.uniform(1200, 1900))
        book.add(MANUAL, end, f"Bankavgifter {year}-{month:02d}", [("6570", fee), ("1930", -fee)])


@dataclass(frozen=True)
class _AnomalySpec:
    id: int
    category: FlagCategory
    description: str
    accounts: list[str]
    also_accepted: list[FlagCategory] = field(default_factory=list)


ANOMALIES = (
    _AnomalySpec(
        1,
        "agarrelaterad",
        "Månatligt konsultarvode 45 TSEK till ägarens bolag Ägarbolaget AB",
        ["6550"],
        ["narstaende"],
    ),
    _AnomalySpec(
        2,
        "engangspost",
        "Kontorsflytt april 2025, fem verifikat om totalt 380 TSEK",
        ["5010", "5410"],
    ),
    _AnomalySpec(3, "agarrelaterad", "Privat bil leasad via bolaget, 8 TSEK per månad", ["5615"]),
    _AnomalySpec(
        4,
        "periodisering",
        "Faktura 600 TSEK daterad 2025-12-30 för leverans januari 2026, obetald vid årsskiftet",
        ["3041"],
    ),
    _AnomalySpec(5, "engangspost", "Försäkringsersättning 250 TSEK för vattenskada", ["3990"]),
    _AnomalySpec(6, "engangspost", "Rekryteringsarvode 150 TSEK till bemanningsbolag", ["6550"]),
)


def _plant_anomalies(book: _Book) -> None:
    """The anomalies in docs/DOMAIN.md section 6, and only those."""
    for year, month in _months():
        book.supplier_invoice(
            dt.date(year, month, 28),
            f"Konsultarvode Ägarbolaget AB {year}-{month:02d}",
            "6550",
            Decimal("45000.00"),
            anomaly=1,
        )
        # Private car: half the VAT is deductible on leased cars.
        book.supplier_invoice(
            dt.date(year, month, 5),
            f"Leasing personbil {year}-{month:02d}",
            "5615",
            Decimal("8000.00"),
            vat_rate=VAT / 2,
            anomaly=3,
        )

    move = (
        (4, "5010", "95000.00", "Dubbel hyra gamla lokalen under flytt"),
        (9, "5010", "60000.00", "Flyttstädning gamla lokalen"),
        (14, "5010", "85000.00", "Flyttfirma kontorsflytt"),
        (22, "5410", "110000.00", "Kontorsmöbler nya lokalen"),
        (28, "5410", "30000.00", "Skärmar och dockningsstationer nya kontoret"),
    )
    for day, account, amount, text in move:
        book.supplier_invoice(dt.date(2025, 4, day), text, account, Decimal(amount), anomaly=2)

    book.customer_invoice(
        dt.date(2025, 12, 30),
        "Faktura 26001 kund 1077, avser leverans januari 2026",
        Decimal("600000.00"),
        anomaly=4,
        paid=False,
    )
    book.add(
        MANUAL,
        dt.date(2024, 10, 15),
        "Försäkringsersättning vattenskada kontor",
        [("1930", Decimal("250000.00")), ("3990", Decimal("-250000.00"))],
        anomaly=5,
    )
    book.supplier_invoice(
        dt.date(2025, 2, 17),
        "Rekryteringsarvode Bemanningsbolaget AB, ekonomichef",
        "6550",
        Decimal("150000.00"),
        anomaly=6,
    )


def _facit(seed: int, ledger: Ledger, refs: dict[int, list[str]]) -> Facit:
    anomalies = []
    for spec in ANOMALIES:
        wanted = set(refs[spec.id])
        total = sum(
            (
                t.amount
                for v in ledger.vouchers
                if v.ref in wanted
                for t in v.transactions
                if t.account in spec.accounts
            ),
            ZERO,
        )
        anomalies.append(
            PlantedAnomaly(
                id=spec.id,
                category=spec.category,
                also_accepted=spec.also_accepted,
                description=spec.description,
                voucher_refs=refs[spec.id],
                accounts=spec.accounts,
                total_amount=total,
            )
        )
    return Facit(seed=seed, anomalies=anomalies)


def _vat(book: _Book) -> None:
    """Monthly VAT return on the last day of the month, paid the 12th of the next month."""
    for year, month in _months():
        output = ZERO
        deductible = ZERO
        for d in book.drafts:
            if (d.date.year, d.date.month) == (year, month):
                for account, amount in d.lines:
                    if account == "2611":
                        output += amount
                    elif account == "2641":
                        deductible += amount
        net = output + deductible  # raw signs: negative means VAT to pay
        book.add(
            MANUAL,
            _month_end(year, month),
            f"Momsredovisning {year}-{month:02d}",
            [("2611", -output), ("2641", -deductible), ("2650", net)],
        )
        due = _next_month(year, month, 12)
        if due <= LAST_DAY:
            book.add(
                MANUAL, due, f"Betalning moms {year}-{month:02d}", [("2650", -net), ("1930", net)]
            )


def _close_2024(book: _Book) -> None:
    """Book 2024 tax and transfer the result to equity; 2025 is left open (before closing)."""
    result = ZERO
    for d in book.drafts:
        if d.date.year == 2024:
            result += sum((a for acc, a in d.lines if acc[0] in "345678"), ZERO)
    tax = _round(-result * CORPORATE_TAX)
    book.add(MANUAL, dt.date(2024, 12, 31), "Beräknad skatt 2024", [("8910", tax), ("2512", -tax)])
    profit_after_tax = -(result + tax)
    book.add(
        MANUAL,
        dt.date(2024, 12, 31),
        "Årets resultat 2024",
        [("8999", profit_after_tax), ("2099", -profit_after_tax)],
    )
    book.add(
        MANUAL,
        dt.date(2025, 5, 12),
        "Betalning slutlig skatt 2024",
        [("2512", tax), ("1930", -tax)],
    )


_ACCOUNT_TYPES: dict[str, AccountType] = {"1": "T", "2": "S", "3": "I"}


def _account_type(number: str) -> AccountType:
    return _ACCOUNT_TYPES.get(number[0], "K")


def _assemble(drafts: list[_Draft]) -> tuple[Ledger, dict[int, list[str]]]:
    numbers: dict[str, int] = {}
    vouchers: list[Voucher] = []
    refs: dict[int, list[str]] = {}
    for d in sorted(drafts, key=lambda d: (d.date, d.seq)):
        numbers[d.series] = numbers.get(d.series, 0) + 1
        voucher = Voucher(
            series=d.series,
            number=str(numbers[d.series]),
            date=d.date,
            text=d.text,
            reg_date=d.date,
            transactions=[Transaction(account=a, amount=amt) for a, amt in d.lines],
        )
        vouchers.append(voucher)
        if d.anomaly is not None:
            refs.setdefault(d.anomaly, []).append(voucher.ref)
    vouchers.sort(key=lambda v: (v.series, int(v.number)))

    opening: list[Balance] = []
    closing: list[Balance] = []
    results: list[Balance] = []
    ib = dict(OPENING_2024)
    ib["2091"] = -sum(ib.values(), ZERO)
    for year, index in ((2024, -1), (2025, 0)):
        moved: dict[str, Decimal] = {}
        for v in vouchers:
            if v.date.year == year:
                for t in v.transactions:
                    moved[t.account] = moved.get(t.account, ZERO) + t.amount
        ub = dict(ib)
        for account, amount in moved.items():
            if account[0] in "12":
                ub[account] = ub.get(account, ZERO) + amount
        opening += [Balance(year_index=index, account=a, amount=ib[a]) for a in sorted(ib)]
        closing += [Balance(year_index=index, account=a, amount=ub[a]) for a in sorted(ub)]
        results += [
            Balance(year_index=index, account=a, amount=moved[a])
            for a in sorted(moved)
            if a[0] in "345678"
        ]
        ib = ub

    ledger = Ledger(
        company_name=COMPANY_NAME,
        org_nr=ORG_NR,
        program=PROGRAM,
        generated=GENERATED,
        chart_type="BAS2014",
        fiscal_years=[
            FiscalYear(index=0, start=dt.date(2025, 1, 1), end=dt.date(2025, 12, 31)),
            FiscalYear(index=-1, start=dt.date(2024, 1, 1), end=dt.date(2024, 12, 31)),
        ],
        accounts={
            n: Account(number=n, name=name, type=_account_type(n)) for n, name in ACCOUNTS.items()
        },
        opening_balances=opening,
        closing_balances=closing,
        results=results,
        vouchers=vouchers,
    )
    return ledger, refs


def build_demo_company(seed: int) -> tuple[Ledger, Facit]:
    """Build the demo ledger and its facit in memory. Same seed, same result."""
    book = _Book(random.Random(seed))
    _opening_settlements(book)
    _sales(book)
    _purchases(book)
    _payroll(book)
    _financing_and_depreciation(book)
    _plant_anomalies(book)
    _vat(book)
    _close_2024(book)
    ledger, refs = _assemble(book.drafts)
    return ledger, _facit(seed, ledger, refs)


def generate_demo_company(seed: int, out: Path) -> Facit:
    """Write a SIE4 file to `out` and `facit.json` next to it; return the facit."""
    ledger, facit = build_demo_company(seed)
    write_sie(ledger, out)
    (out.parent / "facit.json").write_text(facit.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return facit

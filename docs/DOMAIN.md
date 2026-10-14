# Domain reference

Source of truth for all finance logic. Change this file deliberately and log why in
DECISIONS.md.

## 1. SIE4 format

Plain text, one record per line, encoding CP437 ("#FORMAT PC8"). Fields are separated by
spaces; strings with spaces are quoted with `"` and an embedded quote is written `\"`.
Object lists are in braces `{}`.

Tags the parser must support:

| Tag | Example | Meaning |
|---|---|---|
| `#FLAGGA` | `#FLAGGA 0` | import flag, ignore value |
| `#PROGRAM` | `#PROGRAM "Fortnox" 3.0` | generating program |
| `#FORMAT` | `#FORMAT PC8` | encoding marker |
| `#GEN` | `#GEN 20260101` | generation date |
| `#SIETYP` | `#SIETYP 4` | must be 4 |
| `#FNAMN` | `#FNAMN "Bolaget AB"` | company name |
| `#ORGNR` | `#ORGNR 999999-9999` | org number |
| `#RAR` | `#RAR 0 20250101 20251231` | fiscal year; 0 = current, -1 = previous |
| `#KPTYP` | `#KPTYP BAS2014` | chart of accounts type |
| `#KONTO` | `#KONTO 1930 "Företagskonto"` | account name |
| `#KTYP` | `#KTYP 1930 T` | account type (T, S, K, I), optional |
| `#IB` | `#IB 0 1930 100000.00` | opening balance, year index, account, amount |
| `#UB` | `#UB 0 1930 145000.00` | closing balance |
| `#RES` | `#RES 0 3041 -100000.00` | result account balance for the year |
| `#PSALDO` | `#PSALDO 0 202501 3041 {} -100000.00` | period balance, optional |
| `#VER` | `#VER A 1 20250115 "text" 20250116` | voucher: series, number, date, text, optional reg date |
| `#TRANS` | `#TRANS 1930 {} -50000.00 20250315 "text"` | transaction: account, objects, amount, optional date and text |

`#VER` is followed by `{` on its own line, one `#TRANS` per line, then `}`.
Unknown tags are ignored with a warning, never an error.

Signs: debit positive, credit negative. Revenue and liabilities are negative in raw data.

Integrity rules:
- Every voucher balances: sum of its `#TRANS` amounts = 0.00
- Balance accounts (class 1 and 2): IB + sum(TRANS in year) = UB
- Result accounts (class 3 to 8): sum(TRANS in year) = RES
- Before year end closing, sum of all UB = minus the period result

## 2. BAS mapping (account number ranges)

| Range | Line | Statement |
|---|---|---|
| 1000 to 1399 | Anläggningstillgångar | BR |
| 1400 to 1499 | Lager | BR |
| 1500 to 1599 | Kundfordringar | BR |
| 1600 to 1799 | Övriga kortfristiga fordringar och förutbetalda kostnader | BR |
| 1800 to 1899 | Kortfristiga placeringar | BR |
| 1900 to 1999 | Kassa och bank | BR |
| 2000 to 2099 | Eget kapital | BR |
| 2100 to 2199 | Obeskattade reserver | BR |
| 2200 to 2299 | Avsättningar | BR |
| 2300 to 2399 | Långfristiga skulder | BR |
| 2400 to 2499 | Leverantörsskulder och kortfristiga skulder till kreditinstitut | BR |
| 2500 to 2599 | Skatteskulder | BR |
| 2600 to 2699 | Moms och punktskatter | BR |
| 2700 to 2799 | Personalens skatter och avgifter | BR |
| 2800 to 2899 | Övriga kortfristiga skulder | BR |
| 2900 to 2999 | Upplupna kostnader och förutbetalda intäkter | BR |
| 3000 to 3699 | Nettoomsättning | RR |
| 3700 to 3999 | Övriga rörelseintäkter (3740 öresavrundning) | RR |
| 4000 to 4999 | Råvaror, handelsvaror och underentreprenader | RR |
| 5000 to 6999 | Övriga externa kostnader | RR |
| 7000 to 7699 | Personalkostnader | RR |
| 7700 to 7899 | Av och nedskrivningar | RR |
| 7900 to 7999 | Övriga rörelsekostnader | RR |
| 8000 to 8799 | Finansiella poster | RR |
| 8800 to 8899 | Bokslutsdispositioner | RR |
| 8900 to 8999 | Skatt och årets resultat | RR |

Special cases (log in DECISIONS.md if changed): 2410 and 2417 are short term bank debt,
counted in net debt not NWC; 2350 to 2359 long term bank loans in net debt.

## 3. Statements

- Monthly income statement: sum of TRANS per mapped line per calendar month, using the
  voucher date.
- Monthly balance sheet: IB + cumulative TRANS per account at month end.
- Presentation: flip signs so revenue, liabilities and equity show positive.

## 4. KPIs

- Nettoomsättning = minus sum(3000 to 3699)
- Bruttoresultat = Nettoomsättning minus (4000 to 4999)
- EBITDA = rörelseresultat before 7700 to 7899, i.e. minus sum(3000 to 7699 and 7900 to 7999)
- EBITDA marginal = EBITDA / Nettoomsättning
- EBIT = EBITDA minus (7700 to 7899)
- LTM = sum of the last 12 months
- Tillväxt = LTM vs the 12 months before

## 5. Working capital and net debt

- NWC = Lager + Kundfordringar + Övriga kortfristiga fordringar minus Leverantörsskulder
  (excluding 2410, 2417) minus Moms minus Personalens skatter minus Upplupna kostnader
  minus Övriga kortfristiga skulder (excluding owner and group loans, flagged)
- NWC normal level = average of the last 12 month end values
- Net debt = interest bearing debt (2350 to 2359, 2410, 2417, leasing if booked) plus
  tax debt (2500 to 2599) minus kassa och bank (1900 to 1999) minus kortfristiga
  placeringar (1800 to 1899)

## 6. Normalization categories and planted anomalies

Categories for `Flag.category`:

- `engangspost` one off cost or income (move, legal dispute, recruitment fee)
- `agarrelaterad` owner related cost (private car, family salary, owner's company fee)
- `narstaende` related party transaction at non market terms
- `periodisering` misplaced period (revenue or cost booked in wrong month or year)
- `avvikelse` unusual pattern worth a question but not a normalization (spike, reversal)
- `normal` no action

Planted anomalies in the demo company (generator must create exactly these and log them in
`data/demo/facit.json`):

1. Konsultarvode to "Ägarbolaget AB" every month, 6550, 45 TSEK, `agarrelaterad`/`narstaende`
2. Office move, 5 vouchers in one month, 5010 and 5410, 380 TSEK total, `engangspost`
3. Private car leasing booked on 5615, 8 TSEK per month, `agarrelaterad`
4. Kundfordringar spike in December 2025: large invoice (600 TSEK) booked 2025-12-30 with
   text referring to delivery in January 2026, still unpaid at year end, `periodisering`
5. Insurance payout 250 TSEK on 3990 in one month, `engangspost`
6. Recruitment fee 150 TSEK on 6550 to a staffing firm, `engangspost`

## 7. Glossary

- LTM: last twelve months
- NWC: net working capital (rörelsekapital)
- FDD: financial due diligence
- QoE: quality of earnings, the normalized EBITDA view
- TSEK: thousand SEK

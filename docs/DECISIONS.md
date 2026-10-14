# Decisions

Format: `YYYY-MM-DD` decision, reason.

- 2026-10-14 SIE4 bookkeeping as primary input instead of registry data. Reason: it is what
  Transaction Services actually receives, needs no paid API, and demos offline.
- 2026-10-14 Deterministic numbers, LLM only for interpretation. Reason: traceability and
  trust in client work.
- 2026-10-14 Synthetic demo company only. Reason: no confidentiality risk in the demo.
- 2026-10-14 Free Bolagsverket "värdefulla datamängder" API plus SCB API for screening.
  Reason: the full Bolagsverket company information API is paid and needs an agreement.
- 2026-10-14 Demo SIE file holds both fiscal years 2024 (#RAR -1) and 2025 (#RAR 0) with
  vouchers for all 24 months. Reason: one file gives LTM and growth without multi-file
  input. Transactions are assigned to a fiscal year by voucher date.
- 2026-10-14 Voucher numbers run continuously per series across both years in the demo file.
  Reason: refs such as `D18` must be unique within a file for facit and source references.
- 2026-10-14 2024 is closed in the demo file (tax on 8910/2512, result via 8999 to 2099), so
  IB 2025 equals UB 2024; 2025 is left before closing. Reason: matches a real export taken
  after the prior year's closing.
- 2026-10-14 Demo revenue is about 40 MSEK per year (38.5 in 2024, 41.5 plus the planted
  600 TSEK invoice in 2025). Reason: "about 40 MSEK" read as annual, a typical Buy & Build size.
- 2026-10-14 Integrity check: missing #IB, #UB or #RES count as 0.00, and fiscal years without
  vouchers are skipped. Reason: exporters omit zero balances and include prior year balances
  without prior year vouchers. `expected` is the stated value, `actual` the computed one.
- 2026-10-14 `#SIETYP` other than 4 is a parse error; unknown tags are warnings. Reason: types
  1 to 3 lack vouchers, so the analysis cannot run on them.
- 2026-10-14 `#TRANS` object lists are kept as (dimension, object) pairs. Reason: cost centres
  and projects may matter for later analysis; cheap to keep.
- 2026-10-14 Facit `total_amount` uses raw SIE signs and only the anomaly accounts, net of VAT
  (e.g. -600 000.00 on 3041). Anomaly 1 has category `agarrelaterad` with `narstaende` in
  `also_accepted`. Reason: DOMAIN.md gives two categories; the eval should accept either.
- 2026-10-14 The demo company uses 5615, 3990 and 6550 only in planted anomalies. Reason:
  "exactly these" anomalies; normal consulting costs go to 4600 and 6530/6540.

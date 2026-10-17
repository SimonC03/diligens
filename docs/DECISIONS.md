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
- 2026-10-14 Demo VAT is filed monthly and paid the 26th of the next month (turnover above
  40 MSEK); payroll taxes the 12th. Jan and Aug deadline exceptions are ignored.
- 2026-10-14 2025 is open in the demo file: preliminary tax is paid to 2518 but no 2025 tax
  expense is booked. Reason: realistic before closing. M2 must treat the accrued 2025 tax as
  a debt like item, otherwise net debt is understated by about 1.1 MSEK.
- 2026-10-14 Demo särskild löneskatt is accrued monthly on 2514 and paid once a year in May;
  prepaid costs (17xx) are not modelled. 2514 sits in 2500 to 2599, so it lands in net debt
  as a tax item, not in NWC; the holiday pay accrual (2920, 2940) gives NWC its accruals.
- 2026-10-14 Office move furniture (110 TSEK on 5410) stays expensed as DOMAIN.md says, though
  an advisor would normally call it capex. Reason: it is part of the planted one off item.
- 2026-10-14 The generator draws only via `random.Random.random()` and builds integers itself.
  Reason: Python guarantees reproducible `random()` across versions, not `randint`.
- 2026-10-14 DOMAIN.md section 5 extended with explicit BAS lists: interest bearing debt
  adds 2330 to 2349 and 2840; group, related party and dividend debts (2360 to 2399, 2860
  to 2869, 2893, 2898) leave NWC and are listed as debt like items to assess. 2200 to 2329
  are listed the same way so no balance is silently dropped. Reason: owner and group loans
  must not inflate working capital; the advisor decides whether they are debt.
- 2026-10-14 Every open fiscal year gets its own estimated tax line in net debt (20.6 % of
  result before tax so far, floor zero), also a prior year not yet closed. A year is open
  when its result accounts do not sum to zero (no closing to 8999) and no tax is booked on
  8900 to 8989. Reason: otherwise prepaid F-skatt on 2518 lowers net debt while the tax
  expense is missing; booked tax means the company has taken a view (e.g. a loss carry
  forward) that an estimate must not override.
- 2026-10-14 Statements cover every fiscal year from the first to the last one with
  vouchers (a middle year without vouchers stays in, so months are contiguous) and stop at
  the last month with vouchers. Reason: an export taken mid year must not show empty
  trailing months, and LTM, NWC normal level and net debt use that last month. Months
  without transactions are 0. Each year's balance sheet starts from its own #IB. A computed
  line "Årets resultat (beräknat)" makes the balance sheet sum to zero before closing.
- 2026-10-14 KPIs include year to date series per fiscal year next to monthly and LTM.
  Reason: an export with fewer than 12 months has no LTM, and advisors compare YTD.
- 2026-10-14 Reconciliation is `ok` only when it has rows, every difference is 0.00, no
  account is unmapped and no voucher falls outside the fiscal years. Callers (CLI, MCP)
  must treat `ok == False` as a failure. #IB of a year is not compared with #UB of the
  year before, since closing via #IB legitimately moves the result between 2099 and 2091.
- 2026-10-14 KPI values follow DOMAIN.md signs (revenue, costs and EBITDA positive) while
  statements keep raw SIE signs. LTM needs 12 months and growth needs 24; otherwise None.
  Ratios are unrounded Decimals; rounding happens at export.
- 2026-10-14 NWC is symmetric: the receivable side of financing leaves NWC too. Group
  receivables (1660 to 1669), loan receivables (1682) and receivables from owners or
  related parties (1685) are listed for assessment; the tax account and tax receivables
  (1630 to 1649) move to net debt next to 2500 to 2599. The rest of 1680 to 1689 (e.g.
  1684, 1689) stays in NWC as operating items. Reason: a loan to the owner must not
  inflate working capital, mirroring 2893 on the liability side.
- 2026-10-15 LLM output uses structured outputs (`client.messages.create` with
  `output_config.format` built by `anthropic.transform_schema` from a Pydantic model)
  instead of forced tool use. The stop reason is checked before the JSON is validated, so
  refusals and cut off answers are handled and every attempt's tokens are counted. Reason: Claude Sonnet 5.5 rejects forced
  `tool_choice` with a 400; structured outputs give schema valid JSON directly. Business rules
  are checked on top (every voucher once, no unknown refs, no digits in model text) and fed
  back on retry, at most 3 attempts; a batch that still fails falls back to review.
- 2026-10-15 The model never writes a number into the output. Flag account and amount come
  from the voucher's largest EBITDA line; rationales and DD questions are rejected if they
  contain digits, and code appends account, vouchers and TSEK to each question.
- 2026-10-15 Prefilter rules on EBITDA accounts (3000 to 7699, 7900 to 7999), thresholds in
  `agent/prefilter.py`: material amount (0.25 % of annualised revenue, at least 10 TSEK, and
  3x the account median when the account has 6 or more vouchers), round amount (multiple of
  5 TSEK from 25 TSEK), same account, amount and text in 6 or more months, owner related
  account (5610 to 5699) or words (ägar, privat, familj, holding, närstående), month spike
  (2x the mean of other months and at least half the materiality, vouchers outside 25 % of
  the median), revenue within 5 days of year end above materiality or text naming a later
  month, and any line on 3900 to 3999. Demo: 93 of 1 990 vouchers, all 56 planted ones.
- 2026-10-15 EBITDA bridge per category: engångspost and ägarrelaterad add back the voucher's
  net EBITDA lines (owner costs in full, market rate not estimated), periodisering moves the
  item out of the period, närstående and avvikelse give a DD question only. Only flags with
  confidence >= 0.7 dated inside the LTM window adjust; YTD is used when there is no LTM.
- 2026-10-15 Batches of 50 vouchers, effort medium, system prompt plus the company's chart of
  accounts cached as a prefix. Client data sent to the API is limited to the prefiltered
  vouchers; real client use needs zero data retention or a local model (M7).
- 2026-10-15 API errors: connection, rate limit, overload and server errors (after the SDK's
  own retries) stop all further calls in the run and send the remaining items to review,
  shown in the CLI; a rejected key, unknown model or rejected request stops the run with a
  Swedish message. CLI exit codes: 0 done, 1 input or AI configuration error, 2 the
  reconciliation differs.
- 2026-10-15 A recurring series (same account, amount and normalised text in 6 or more
  months) is classified once and the answer applied to every voucher in it. Demo: 93
  prefilter hits become 19 items for the model.
- 2026-10-15 The bridge only adds back vouchers with a single EBITDA account. A collective
  voucher (payroll batch, daily sales journal) with a confident adjusting flag goes to
  review, since adding it back whole would overstate EBITDA.
- 2026-10-15 Owner related prefilter accounts: 5610-5619 (personbilar), 5670-5679 (båt,
  flyg), 6070-6072 (representation), 7220-7229 (löner till företagsledare), 7240-7249
  (styrelsearvoden); words also include förvaltning and invest; advisors can add names with
  `--narstaende`. Cut-off covers fiscal year ends and the LTM end month, credit notes on
  3000-3699 within 10 days after, and correction words (återför, kreditering, rättelse,
  korrigering, makulering). Lines on 3900-3999 and 7970-7979 need a quarter of the
  materiality. Known gap: an item booked just before the LTM window that belongs inside it
  is not moved in.
- 2026-10-15 DD question amounts use the same basis as the bridge (net EBITDA lines) and
  the reference shows the months covered, so they reconcile with the bridge for the LTM.
- 2026-10-15 Model for flagging and questions: `claude-haiku-5-5` (set in .env). The M3
  eval holds on Haiku: two live runs on the demo both found 5 of 6 planted anomalies with
  94 % precision (33 confident flags), in 1 to 2 requests and about 12k input and 6k output
  tokens. The miss is the private car (#3): right category, confidence 0.6, so it lands in
  review with a DD question, which is acceptable. The two false positives are the July
  payroll runs with the holiday pay release, flagged as avvikelse (question, no adjustment).
- 2026-10-15 Known instability: the owner company fee (#1) is classified agarrelaterad in
  one run and narstaende in another. Both are accepted by the facit, but only agarrelaterad
  adjusts EBITDA, so adjusted EBITDA can differ by the fee (540 TSEK LTM on the demo).
  To revisit in M4: show confident narstaende items in the bridge sheet as "ej justerat,
  att bedöma" so the advisor makes the call, rather than relying on the model's choice.
- 2026-10-15 Excel values are written in TSEK as unrounded Decimals (SEK / 1000) with the
  number format `#,##0.0`, which Swedish Excel shows with space thousands and decimal comma.
  Formulas therefore compute exactly; rounding is display only. The reconciliation sheet
  stays in SEK with two decimals so differences of öre are visible.
- 2026-10-15 Excel signs: income statement with revenue positive and costs negative, so
  subtotals are plain sums; balance sheet with assets, equity and liabilities positive and a
  Differens row that must be zero. Working capital keeps raw signs (assets positive,
  liabilities negative) so its total is the plain sum.
- 2026-10-15 The bridge sheet lists every candidate with an Inkludera column (1/0). Confident
  adjustments start at 1; confident related party items and low confidence items of an
  adjusting category start at 0. Justerad EBITDA = reported + SUMPRODUCT(amount, include), so
  the advisor decides in Excel and the model's choice is a proposal. This also neutralises
  the agarrelaterad/narstaende instability seen in the M3 eval.
- 2026-10-15 Memo: code builds named facts with formatted values, the model writes Swedish
  markdown with {placeholders} only, and code fills in the values. Text with digits outside
  placeholders (markdown list numbers excepted), unknown keys, dashes or more than 450 words
  is rejected and retried; a template with the same facts is used offline and on failure.
  Demo run with Haiku: memo accepted on the first attempt, whole analyze run 35 seconds.
- 2026-10-17 MCP tools return Swedish text (markdown tables for lists) rather than JSON,
  since the reader is the model talking to an advisor. Failures the model can act on (wrong
  path, not a SIE file, unknown month, locked output folder, analysis errors) are raised as
  ToolError so they arrive as readable text; nothing is raised as a protocol error.
- 2026-10-17 `med_ai` defaults to on in the MCP tools; without an API key the tools fall
  back to the deterministic run and say so, instead of failing. The analysis is cached per
  resolved path, file modification time, AI setting and related party names, so follow-up
  tools reuse it and a changed file is analysed again.
- 2026-10-17 MCP cache: one analysis per file, guarded by a lock so parallel tool calls on a
  cold cache run it once. A call reuses the cached analysis when the file is unchanged, the
  AI setting matches and the related party names match (or none are given). hamta_nyckeltal
  is deterministic and reuses any cached analysis or runs without AI. exportera_excel only
  writes to output/ inside the project or to folders outside it.
- 2026-10-17 Screening fetches candidates by paging through SCB's SNI list and the turnover
  class lists (targets and their neighbours) and intersecting on peOrgNr, instead of one
  `/full` call per company. SNI 62100 (31 623 units) pages in about 6 s; a full screening
  with 25 annual reports took 20 s.
- 2026-10-17 Hard filters: active (ftgStat 1), jurform 49 (aktiebolag), primary SNI among
  the targets, and turnover and employee class at most one class from a target. Profile
  score 0-60: SNI 15, turnover class 15 (7 one class off), employee class 10 (5 one class
  off, 0 if SCB has no figure), county 10, active aktiebolag 10. Financial score 0-40 from
  the latest digital annual report: growth 20 (>= 10 %) / 10 (0-10 %) / 0, EBIT margin
  20 (>= 10 %) / 10 (5-10 %) / 0. EBIT margin, not EBITDA, since K2 reports need not tag
  depreciation.
- 2026-10-17 Annual reports are looked up only for the 25 best profiles by default
  (`--topp`), in four threads, to stay well under a minute. Others show "Ej hämtad"; no
  digital report shows "Saknas digitalt" and no financial points, never an estimate.
- 2026-10-17 Bolagsverket's "Värdefulla datamängder" has a single production environment,
  so "test environment first" in PLAN.md could not apply; development used the spec's own
  test number (5299999994) and synthetic fixtures.
- 2026-10-17 Fixtures for SCB and Bolagsverket are synthetic: the real responses were used
  once to learn the structure (names, fields, formats, contexts), then rebuilt with made-up
  companies and values. The recorded specs and SCB code tables are stored as they are.
- 2026-10-17 Ties at the annual report cut-off: every candidate with the same profile score
  as the last one in the top list is looked up too, so who gets financial points never
  depends on the company name. Capped at 100 lookups, with a warning in the long list.
- 2026-10-17 Growth earns points only when the prior year revenue is above 0, both years
  are about twelve months (350 to 380 days), EBIT is not negative and the report's year
  ended at most 18 months ago. An older report is shown as "Inaktuell årsredovisning" and
  keeps its margin points. A non twelve month year is noted in the reasons.
- 2026-10-17 Only iXBRL contexts without segment or scenario are read, so figures split by
  business line or other dimensions never replace the company totals.
- 2026-10-17 Lookup outcomes are kept apart: "Saknas digitalt" (no digital report),
  "Kunde inte läsas" (a report exists but could not be parsed), "Kunde inte hämtas"
  (Bolagsverket did not answer) and "Ej hämtad" (outside the top list). An error for one
  company never stops the run. When Bolagsverket is unavailable, the remaining lookups are
  skipped and the long list says so. Only looked-up companies get a rank; a blank
  Nyckeltal means no points could be given, not zero.
- 2026-10-17 Runtime limits for screening: 15 s timeout per Bolagsverket call, at most two
  document packages per company, 25 MB per download and 50 MB per unpacked file, and a
  Retry-After from SCB capped at 10 s. `--topp 0` runs on SCB alone and needs no
  Bolagsverket credentials.
- 2026-10-17 Bolagsverket calls are paced at 4 per second across all screening threads, after
  a real run with 61 tied lookups got HTTP 429 at full speed. Paced, the same run finished
  in 39 s with every lookup answered. A 429 that persists after retries counts as
  "Bolagsverket svarar inte" and stops further lookups, with a message about throttling.

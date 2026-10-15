# Plan

Order: M1 to M5 first, M6 in parallel once API keys arrive, M7 last.

Each milestone has a branch `m<N>-<slug>` and a PR. Criteria are checked off only when verified.

## M0 Setup (done in scaffold)

- [x] Repo structure, docs, CI
- [x] `uv run diligens --help` works
- [x] `uv run pytest` green on smoke tests

## M1 Synthetic data and SIE4 parser

Branch `m1-sie`. Modules: `sie/parser.py`, `sie/generator.py`.

- [x] Parser reads `tests/fixtures/minimal.se` into a `Ledger` and matches every value in
      `tests/fixtures/minimal_expected.json`
- [x] Parser supports all tags in DOMAIN.md section 1, including optional `#TRANS` fields,
      quoted strings with escaped quotes, and CP437 encoding
- [x] Integrity check: for every balance account, IB + sum(TRANS) = UB; for every result
      account, sum(TRANS) = RES. Violations returned as a list, not raised
- [x] Generator builds a fictional service company: 24 months (2024 to 2025), about
      40 MSEK revenue, about 2 000 vouchers, deterministic for a given seed
- [x] Generator plants the anomalies in DOMAIN.md section 6 and writes
      `data/demo/facit.json` with voucher refs for each
- [x] Round trip: generated file parses with zero integrity violations

## M2 Analysis engine

Branch `m2-analysis`. Modules: `analysis/`.

- [x] BAS mapping per DOMAIN.md section 2; unmapped accounts returned explicitly
- [x] Monthly income statement and balance sheet per DOMAIN.md section 3
- [x] LTM, EBITDA, EBITDA margin, gross margin, revenue growth per DOMAIN.md section 4
- [x] Net working capital monthly and normal level (12 month average) per section 5
- [x] Net debt items per section 5
- [x] Reconciliation report: statements vs `#RES` and `#UB`, difference 0.00 on the demo file
- [x] All functions pure and covered by tests on the minimal fixture

## M3 AI flagging and DD questions

Branch `m3-agent`. Modules: `agent/`, `scripts/eval_flags.py`.

- [x] Flagging over vouchers in batches with schema `Flag` (category, rationale,
      confidence, voucher ref) via structured outputs; Pydantic validation; retries on
      schema errors
- [x] Deterministic prefilters (large amounts, owner related counterparties, round
      amounts, month end spikes) reduce what goes to the LLM; documented in DECISIONS.md
- [x] Adjusted EBITDA bridge uses only flags with confidence >= 0.7; the rest are listed
      as "Kräver granskning"
- [x] DD question list: each question references account, voucher and amount
- [x] `scripts/eval_flags.py` compares flags to facit and prints recall and precision;
      recall >= 5 of 6 planted anomalies
- [x] `--no-llm` still runs end to end
- [x] Unit tests mock the Anthropic client; one `live` test runs the real call

## M4 Excel export and memo

Branch `m4-export`. Modules: `export/excel.py`, `agent/memo.py`.

- [ ] Sheets: Sammanfattning, RR månad, BR månad, EBITDA brygga, Rörelsekapital,
      Nettoskuld, Flaggade poster, DD frågor, Avstämning, Källor
- [ ] Real Excel formulas for totals and the EBITDA bridge, not hardcoded values
- [ ] TSEK formatting, frozen headers, column widths, Swedish labels
- [ ] Every flagged row shows voucher ref and account
- [ ] Memo (markdown, Swedish, max one page) generated from computed numbers only
- [ ] Test: workbook opens with openpyxl, expected sheets exist, formulas present

## M5 MCP server

Branch `m5-mcp`. Module: `mcp_server.py`.

- [ ] Tools: `analysera_sie`, `flagga_poster`, `exportera_excel`, `hamta_nyckeltal`
      (and `screena_bolag`, `hamta_bokslut` after M6)
- [ ] Tool descriptions in Swedish, inputs validated, errors returned as readable text
- [ ] README has the MCP client config snippet and a manual test checklist
- [ ] Manual check: from an MCP client, "Analysera demo_bolag.se och visa de största
      normaliseringsposterna" works end to end

## M6 Buy & Build screening (needs API keys)

Branch `m6-screening`. Modules: `sources/scb.py`, `sources/bolagsverket.py`,
`sources/ixbrl.py`.

- [ ] SCB spec saved to `tests/fixtures/scb/openapi.json`; a test asserts every path and
      response model the client uses exists in the spec
- [ ] SCB code tables (`naringsgrenkoder`, `omsklkoder`, `anstklkoder`, `lankoder`,
      `jurformkoder`, `ftgstatkoder`) recorded as fixtures; open questions in
      DATA_SOURCES.md answered
- [ ] SCB client per DATA_SOURCES.md: `X-API-Key` header, cursor pagination until
      `hasMore` is false, max 5 calls/s with backoff on 429, ProblemDetails mapped to
      readable Swedish errors, nightly 04:00 window handled
- [ ] Screening: SNI endpoint for candidates, `/full` for details, filters in code on
      turnover class, employee class, county, active status and legal form
- [ ] Bolagsverket client: OAuth2 client credentials; organisation lookup, document list,
      document download (zip with iXBRL); test environment first
- [ ] iXBRL parser: `ix:nonFraction` with `name`, `contextRef`, `scale`, `sign`; maps to
      the KPIs in DOMAIN.md section 4
- [ ] Recorded fixtures for all HTTP calls; live tests marked `live`
- [ ] `uv run diligens screen --sni <kod> --oms-kl <kod> --anst-kl <kod> --lan <kod>`
      writes a long list to Excel with source per figure and a fit score with reasons
- [ ] Missing annual reports (paper filed) shown as "Saknas digitalt", never guessed

## M7 Demo polish

- [ ] `docs/DEMO.md` script, 5 minutes, rehearsed
- [ ] Full run under 60 seconds on the laptop
- [ ] Backup screen recording
- [ ] Limitations slide: paper filed reports, BAS mapping per company, client data needs
      zero data retention or local model

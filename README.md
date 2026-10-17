# Diligens

Turns a Swedish SME's bookkeeping (SIE4 export from Fortnox, Visma and others) into a
Financial Due Diligence pack: monthly statements, LTM, EBITDA bridge with normalization
candidates, working capital, net debt, DD questions and a short memo. Every number is
computed in code and traceable to a voucher; the LLM only classifies and writes.

A second module screens Buy & Build targets using the free SCB and Bolagsverket APIs.

The repository contains only synthetic data. A generated demo company with planted
anomalies, and a facit to score against, make every run reproducible.

## Status

Work in progress. See `docs/PLAN.md` for milestones.

## Quick start

```bash
uv sync
cp .env.example .env        # add your keys
uv run diligens generate
uv run diligens analyze data/demo/demo_bolag.se --out output/
```

`--no-llm` runs the deterministic part without an API key.

## Buy & Build screening

Needs `SCB_API_KEY` and `BOLAGSVERKET_CLIENT_ID` / `BOLAGSVERKET_CLIENT_SECRET` in `.env`
(see `docs/DATA_SOURCES.md`).

```bash
uv run diligens screen --sni 62100 --oms-kl 6 --anst-kl 5 --lan 01 --out output/
```

Codes follow SCB's tables: SNI 2025 (five digits), turnover class (6 = 20 to 49 MSEK),
employee class (5 = 20 to 49), county (01 Stockholm). Several values per option are allowed
(`--sni 62100 --sni 62201`). The result is `output/long_list.xlsx`: active aktiebolag with
a fit score 0 to 100 (profile 0 to 60 from SCB, growth and EBIT margin 0 to 40 from the
latest digital annual report), the reasons, and the source of every figure. Annual reports
are fetched for the 25 best profiles (`--topp`); "Saknas digitalt" means the company has no
digital annual report. `--utan-bokslut` uses SCB only.

## Use as an MCP server

The pipeline is also exposed as MCP tools over stdio (`uv run diligens-mcp`):

| Tool | What it returns |
|---|---|
| `analysera_sie` | Reconciliation status, revenue, EBITDA, adjusted EBITDA (LTM or year to date), working capital, net debt, the bridge items and how many items need review |
| `flagga_poster` | Flagged vouchers as a table: voucher, date, account, amount, category, confidence, bridge status, rationale. Filter on status, largest first |
| `hamta_nyckeltal` | KPIs for one month (month, year to date, LTM), working capital and net debt items |
| `exportera_excel` | Writes `dd_pack.xlsx` and `memo.md` and returns the paths and the memo |
| `screena_bolag` | Buy & Build screening: top candidates with score and reasons, optionally the long list |
| `hamta_bokslut` | Key figures from a company's latest digital annual report, with sources |

Every tool takes `sie_fil` (path to the SIE4 file). `med_ai` (default on) uses Claude for
classification, questions and memo; without an API key the tools run without AI and say so.
An analysis is cached per file, so follow-up questions do not repeat the AI calls. Errors
(missing file, not a SIE file, unknown month, locked output file) come back as readable text.

### Claude Desktop

Add to `claude_desktop_config.json` (Settings, Developer, Edit config) and restart Claude
Desktop. On Windows use forward slashes or escaped backslashes in the path. If Claude Desktop
cannot find `uv`, put the full path to `uv.exe` in `command` (`where uv` shows it). The
`--directory` argument makes the server read `.env` and write `output/` in the project:

```json
{
  "mcpServers": {
    "diligens": {
      "command": "uv",
      "args": ["--directory", "C:/ABSOLUTE/PATH/TO/diligens", "run", "diligens-mcp"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add diligens -- uv --directory C:/ABSOLUTE/PATH/TO/diligens run diligens-mcp
```

Exports go to `output/` in the project (gitignored) or to a folder outside it; other folders
inside the project are refused so client data never ends up in git.

### Manual test checklist

1. `uv run diligens generate --out data/demo/demo_bolag.se` (demo company and facit).
2. Start the client with the server registered; `diligens` lists four tools.
3. Ask: "Analysera data/demo/demo_bolag.se och visa de största normaliseringsposterna."
   Expect a reconciliation "OK", revenue and EBITDA in TSEK, and the bridge items (with AI:
   office move, recruitment fee, the December invoice moved out, owner related fee).
4. Ask: "Vilka poster kräver granskning?" The client calls `flagga_poster` with status
   `granskning`; every row has voucher and account. The answer comes from the cache.
5. Ask: "Visa nyckeltalen för 2025-06." Month, year to date and LTM columns.
6. Ask: "Exportera DD-underlaget till output." `output/dd_pack.xlsx` and `output/memo.md`
   exist; the memo is shown.
7. Error paths: ask for a file that does not exist, and for month 2031-01. Both answers
   explain what to do instead, no stack trace.
8. Optional: remove `ANTHROPIC_API_KEY` from `.env`, restart the server, repeat step 3. The
   answer says the analysis ran without AI.

## Docs

- `docs/PLAN.md` milestones and acceptance criteria
- `docs/DOMAIN.md` finance definitions (SIE4, BAS, KPIs, normalization)
- `docs/ARCHITECTURE.md` modules and data flow
- `docs/DATA_SOURCES.md` SCB and Bolagsverket
- `docs/DEMO.md` five minute walkthrough

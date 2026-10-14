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

## Use as an MCP server

The pipeline is also exposed as MCP tools. Register the server in your MCP client:

```json
{
  "mcpServers": {
    "diligens": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/diligens", "run", "diligens-mcp"]
    }
  }
}
```

Then ask: "Analysera data/demo/demo_bolag.se och visa de största normaliseringsposterna."

## Docs

- `docs/PLAN.md` milestones and acceptance criteria
- `docs/DOMAIN.md` finance definitions (SIE4, BAS, KPIs, normalization)
- `docs/ARCHITECTURE.md` modules and data flow
- `docs/DATA_SOURCES.md` SCB and Bolagsverket
- `docs/DEMO.md` five minute walkthrough

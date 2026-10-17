# Architecture

## Data flow

```
          SIE4 file (CP437)                     SCB API      Bolagsverket API
                 │                                 │                │
          sie/parser.py ──► Ledger           sources/scb.py   sources/bolagsverket.py
                 │                                 │                │ zip with iXBRL
                 │                                 └──► candidates  sources/ixbrl.py
                 ▼                                                  │
     analysis/  (pure functions)  ◄─────────────────────────────────┘
       mapping.py     account → line
       statements.py  monthly RR/BR, LTM
       kpis.py        EBITDA, margins, NWC, net debt
       reconcile.py   statements vs #RES/#UB
                 │
                 ▼
     agent/  (only Anthropic calls)
       prefilter.py   deterministic candidates
       flagging.py    LLM classification → list[Flag]
       questions.py   DD questions → list[DDQuestion]
       memo.py        Swedish memo text from computed numbers
                 │
                 ▼
     export/excel.py ──► output/dd_pack.xlsx, output/memo.md
                 │
     cli.py, mcp_server.py and web/app.py all call the same pipeline functions
```

## Web app (`web/`)

```
browser ──► web/app.py (FastAPI, 127.0.0.1 only; wires everything below)
              ├─ security.py   same-origin writes, no framing, CSP, upload size
              ├─ context.py    Ctx: settings, workspace, jobs, sources, page rendering
              ├─ routes/       general.py, analyses.py, screening.py
              ├─ jobs.py       background runs with stages (ANALYSIS_STAGES, SCREENING_STAGES)
              ├─ workspace.py  analyses and screenings as files in DILIGENS_WORKSPACE
              ├─ views.py      pure: results to table rows, tab contexts, chart, memo HTML
              └─ templates/ static/  server rendered pages, small progressive JS
```

```python
# web/app.py
def create_app(
    settings_provider,
    workspace,
    scb_factory,
    bolagsverket_factory,
    inline_jobs=False,
    demo_file=...,
) -> FastAPI: ...


# analysis/bridge.py
def with_selection(bridge: EbitdaBridge, included: Sequence[bool]) -> EbitdaBridge: ...


# screening.py
def retry_unavailable(result, bolagsverket, today=None, on_report=None) -> ScreeningResult: ...
```

The browser never computes a figure: the bridge check boxes post the choice and get the
totals back from `with_selection`. Subtotals in the web statements are sums of computed
lines and are tested against the KPIs. Hosts other than localhost and state changing
requests from other sites are refused.

## Module contracts

```python
# sie/parser.py
def parse_sie(path: Path) -> Ledger: ...
def check_integrity(ledger: Ledger) -> list[IntegrityIssue]: ...


# sie/generator.py
def generate_demo_company(seed: int, out: Path) -> Facit: ...


# analysis/
def map_accounts(ledger: Ledger) -> Mapping: ...  # includes unmapped list
def monthly_statements(ledger: Ledger, mapping: Mapping) -> Statements: ...
def compute_kpis(statements: Statements, tax_rate: Decimal = ...) -> KPIs: ...
def reconcile(ledger: Ledger, statements: Statements) -> Reconciliation: ...
def ebitda_bridge(kpis: KPIs, ledger: Ledger, flags: list[Flag]) -> EbitdaBridge: ...


# agent/
def prefilter(
    ledger: Ledger, statements: Statements, related_parties: Sequence[str] = ()
) -> list[PrefilterHit]: ...
def flag_vouchers(
    hits: list[PrefilterHit], ledger: Ledger, client: Anthropic | None, model: str
) -> FlaggingResult: ...
def dd_questions(
    flags: list[Flag], ledger: Ledger, client: Anthropic | None, model: str
) -> list[DDQuestion]: ...
def write_memo(result: AnalysisResult, client: Anthropic | None, model: str) -> str: ...


# sources/ (the only place with HTTP)
class ScbClient:            # legal_units(kind, code), count, full(org_nr), code_table(name)
class BolagsverketClient:   # documents(org_nr), download(id), latest_report(org_nr)
def parse_ixbrl(document: bytes, document_id: str) -> AnnualReport: ...


# analysis/screening.py (pure)
def select_candidates(units, turnover, criteria) -> list[Candidate]: ...
def financial_score(report: AnnualReport | None) -> FinancialScore: ...


# screening.py
def run_screening(criteria, scb, bolagsverket, top=25) -> ScreeningResult: ...


# export/longlist.py
def export_long_list(result: ScreeningResult, out_dir: Path) -> Path: ...


# export/excel.py
def export_dd_pack(result: AnalysisResult, out_dir: Path) -> Path: ...


# pipeline.py
def run_analysis(
    sie_path: Path,
    use_llm: bool,
    client: Anthropic | None = None,
    settings: Settings | None = None,
    related_parties: Sequence[str] = (),
    progress: Callable[[str], None] | None = None,  # each of ANALYSIS_STAGES
) -> AnalysisResult: ...
```

`client=None` means deterministic mode: no LLM calls, prefilter hits become
`avvikelse` flags with `needs_review=True`.

## Design rules

- Pydantic models in `models.py` are the only types crossing module boundaries.
- `analysis/` has no IO and no LLM.
- HTTP only in `sources/`, Anthropic only in `agent/`. Both injectable for tests.
- Every `Flag`, KPI and Excel row carries a `source_ref`.

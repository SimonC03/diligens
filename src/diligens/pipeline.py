"""The full analysis run. The CLI and the MCP server both call run_analysis."""

from pathlib import Path

import anthropic

from diligens.agent.flagging import flag_vouchers
from diligens.agent.prefilter import prefilter
from diligens.agent.questions import dd_questions
from diligens.analysis.bridge import ebitda_bridge
from diligens.analysis.kpis import compute_kpis
from diligens.analysis.mapping import map_accounts
from diligens.analysis.reconcile import reconcile
from diligens.analysis.statements import monthly_statements
from diligens.config import DEFAULT_MODEL, Settings, get_settings
from diligens.models import AnalysisResult
from diligens.sie.parser import check_integrity, parse_sie


def run_analysis(
    sie_path: Path,
    use_llm: bool,
    client: anthropic.Anthropic | None = None,
    settings: Settings | None = None,
) -> AnalysisResult:
    """Parse, analyse, flag and question one SIE4 file.

    With `use_llm=False` nothing leaves the machine: prefilter hits become flags for review
    and DD questions use templates. A `client` can be injected, e.g. in tests.
    """
    model: str | None = None
    if use_llm:
        settings = settings or get_settings()
        model = settings.diligens_model
        if client is None:
            if settings.anthropic_api_key is None:
                raise ValueError(
                    "ANTHROPIC_API_KEY saknas i .env. Lägg till nyckeln eller kör med --no-llm."
                )
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    else:
        client = None

    ledger = parse_sie(sie_path)
    mapping = map_accounts(ledger)
    statements = monthly_statements(ledger, mapping)
    kpis = compute_kpis(statements)
    hits = prefilter(ledger, statements)
    flagging = flag_vouchers(hits, ledger, client, model=model or DEFAULT_MODEL)
    bridge = ebitda_bridge(kpis, ledger, flagging.flags)
    questions = dd_questions(bridge.flags, ledger, client, model=model or DEFAULT_MODEL)
    return AnalysisResult(
        source=sie_path.name,
        company_name=ledger.company_name,
        org_nr=ledger.org_nr,
        llm_used=client is not None,
        model=model,
        integrity_issues=check_integrity(ledger),
        mapping=mapping,
        statements=statements,
        kpis=kpis,
        reconciliation=reconcile(ledger, statements),
        prefilter_hits=hits,
        flagging=flagging,
        bridge=bridge,
        questions=questions,
    )

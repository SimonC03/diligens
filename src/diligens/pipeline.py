"""The full analysis run. The CLI and the MCP server both call run_analysis."""

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import anthropic

from diligens.agent.client import make_client
from diligens.agent.flagging import flag_vouchers
from diligens.agent.prefilter import prefilter
from diligens.agent.questions import dd_questions
from diligens.analysis.bridge import ebitda_bridge
from diligens.analysis.kpis import compute_kpis
from diligens.analysis.mapping import map_accounts
from diligens.analysis.reconcile import reconcile
from diligens.analysis.statements import monthly_statements
from diligens.config import DEFAULT_MODEL, Settings, get_settings
from diligens.models import AnalysisResult, Ledger
from diligens.sie.parser import check_integrity, parse_sie


def _require_unique_refs(ledger: Ledger) -> None:
    """Flags, the bridge and questions key on voucher refs; duplicates would merge silently."""
    counts = Counter(v.ref for v in ledger.vouchers)
    duplicated = sorted(ref for ref, n in counts.items() if n > 1)
    if duplicated:
        shown = ", ".join(duplicated[:10]) + (" m.fl." if len(duplicated) > 10 else "")
        raise ValueError(
            f"Verifikationsnummer förekommer flera gånger: {shown}. Exportera ett räkenskapsår "
            "per fil eller numrera serierna löpande."
        )


def run_analysis(
    sie_path: Path,
    use_llm: bool,
    client: anthropic.Anthropic | None = None,
    settings: Settings | None = None,
    related_parties: Sequence[str] = (),
) -> AnalysisResult:
    """Parse, analyse, flag and question one SIE4 file.

    With `use_llm=False` nothing leaves the machine: prefilter hits become flags for review
    and DD questions use templates. A `client` can be injected, e.g. in tests. Raises
    ValueError for unusable input and LlmError when the API rejects the configuration.
    """
    model = DEFAULT_MODEL
    if use_llm:
        settings = settings or get_settings()
        model = settings.diligens_model
        client = client or make_client(settings)
    else:
        client = None

    ledger = parse_sie(sie_path)
    _require_unique_refs(ledger)
    mapping = map_accounts(ledger)
    statements = monthly_statements(ledger, mapping)
    kpis = compute_kpis(statements)
    hits = prefilter(ledger, statements, related_parties)
    flagging = flag_vouchers(hits, ledger, client, model=model)
    bridge = ebitda_bridge(kpis, ledger, flagging.flags)
    questions = dd_questions(bridge.flags, ledger, client, model=model)
    return AnalysisResult(
        source=sie_path.name,
        company_name=ledger.company_name,
        org_nr=ledger.org_nr,
        llm_used=client is not None,
        model=model if client is not None else None,
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

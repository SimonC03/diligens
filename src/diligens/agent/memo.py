"""One page memo in Swedish markdown, written from computed numbers only.

Code turns the analysis into named facts with formatted values ({ebitda} = "5 331,1 TSEK").
Claude writes prose with those placeholders; the text is rejected if it contains digits
outside placeholders, unknown keys or dashes as punctuation, and code fills in the values.
Without a client, or when the model fails, a template uses the same facts.
"""

import json
import logging
import re
from collections.abc import Callable
from decimal import Decimal

import anthropic
from pydantic import BaseModel

from diligens.agent._llm import LlmUnavailable, Usage, ask_structured
from diligens.config import DEFAULT_MODEL
from diligens.formatting import format_tsek
from diligens.models import AnalysisResult

log = logging.getLogger(__name__)

MAX_WORDS = 450
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_LIST_NUMBER = re.compile(
    r"^[ \t]*\d{1,2}\.[ \t]", re.MULTILINE
)  # markdown list markers are not data
# A dash between words, not a list marker at the start of a line.
_DASH = re.compile(r"(?<=\S)[ \t][-–—][ \t]")

SYSTEM_PROMPT = """\
Du skriver ett kort memo på svenska till en rådgivare i en finansiell due diligence av ett
svenskt ägarlett bolag. Underlaget är en lista med fakta där varje fakta har en nyckel och ett
färdigt formaterat värde.

Skriv memot i markdown med rubrikerna ## Sammanfattning, ## Normaliseringar,
## Rörelsekapital och nettoskuld, ## Underlagets kvalitet och ## Frågor till säljaren.
Högst en sida, ungefär trehundra ord. Professionell och saklig ton, korta meningar.

Varje siffra, belopp, procentsats, datum, namn på bolaget och fråga ska skrivas som
platshållare med nyckeln inom klammerparenteser, till exempel {ebitda}. Skriv aldrig
siffror själv. Använd bara nycklar som finns i underlaget. Använd punktlistor med
bindestreck, inte numrerade listor. Använd inte tankstreck som skiljetecken. Påstå inget som
inte följer av underlaget.
"""


class _Memo(BaseModel):
    markdown: str


def _tsek(value: Decimal | None) -> str | None:
    return None if value is None else f"{format_tsek(value)} TSEK"


def _percent(value: Decimal | None) -> str | None:
    return None if value is None else f"{value * 100:.1f} %".replace(".", ",")


def _signed(value: Decimal) -> str:
    return ("+" if value > 0 else "") + f"{format_tsek(value)} TSEK"


MAX_POSTS = 5


def memo_facts(result: AnalysisResult) -> dict[str, str]:
    """Named facts with values formatted for the advisor. The only source of numbers."""
    kpis, bridge = result.kpis, result.bridge
    month = bridge.month
    series = kpis.ltm if bridge.basis == "LTM" else kpis.ytd
    basis = "LTM" if bridge.basis == "LTM" else "hittills i år"
    last = kpis.months[-1]
    taxes = [i for i in kpis.net_debt_items if i.kind == "beraknad_skatt"]
    estimated_tax = (
        f", inklusive beräknad ej bokförd skatt om "
        f"{format_tsek(sum((i.amount for i in taxes), Decimal(0)))} TSEK"
        if taxes
        else ""
    )
    facts: dict[str, str | None] = {
        "bolag": result.company_name,
        "orgnr": result.org_nr,
        "period": f"{kpis.months[0]} till {last}",
        "bas": f"{basis} {bridge.period[0]} till {bridge.period[1]}",
        "omsattning": _tsek(series["nettoomsattning"].values[month]),
        "ebitda": _tsek(series["ebitda"].values[month]),
        "ebitda_marginal": _percent(series["ebitda_marginal"].values[month]),
        "justerad_ebitda": _tsek(bridge.adjusted),
        "justering": _signed(bridge.adjusted - bridge.reported),
        "tillvaxt": _percent(kpis.ltm["tillvaxt"].values.get(last)),
        "rorelsekapital": _tsek(kpis.nwc.values[last]),
        "rorelsekapital_normal": _tsek(kpis.nwc_normal.value),
        "nettoskuld": (
            f"en nettokassa om {format_tsek(-kpis.net_debt)} TSEK"
            if kpis.net_debt < 0
            else f"en nettoskuld om {format_tsek(kpis.net_debt)} TSEK"
        )
        + estimated_tax,
        "nettoskuld_datum": kpis.net_debt_month,
        "avstamning": (
            "ingen differens mot SIE-filens saldon"
            if result.reconciliation.ok
            else "differenser mot SIE-filens saldon som måste utredas"
        ),
        "antal_flaggor": str(len(result.flags)),
        "antal_granskning": str(len(bridge.review)),
        "antal_fragor": str(len(result.questions)),
    }
    items = sorted(bridge.items, key=lambda i: -abs(i.amount))
    facts["urval"] = ", varav de största" if len(items) > MAX_POSTS else ""
    for n, item in enumerate(items[:MAX_POSTS], start=1):
        facts[f"post_{n}"] = f"{item.label}, EBITDA-effekt {_signed(item.amount)}"
    for n, item in enumerate(sorted(bridge.candidates, key=lambda i: -abs(i.amount))[:2], 1):
        facts[f"kandidat_{n}"] = (
            f"{item.label}, EBITDA-effekt om den tas med {_signed(item.amount)}"
        )
    for n, question in enumerate(result.questions[:3], start=1):
        facts[f"fraga_{n}"] = question.wording or question.text
    return {k: v for k, v in facts.items() if v is not None}


def _fill(markdown: str, facts: dict[str, str]) -> str:
    """Put the values in. A fact the data does not allow (e.g. a margin without revenue)
    reads as "uppgift saknas" rather than failing."""
    return (
        _PLACEHOLDER.sub(lambda m: facts.get(m.group(1), "uppgift saknas"), markdown).strip() + "\n"
    )


def _template(facts: dict[str, str]) -> str:
    lines = [
        "## Sammanfattning",
        "",
        "{bolag} ({orgnr}) har analyserats för perioden {period}. Nettoomsättningen uppgick "
        "till {omsattning} och EBITDA till {ebitda}"
        + (", en marginal om {ebitda_marginal}" if "ebitda_marginal" in facts else "")
        + " ({bas})."
        + (
            " Omsättningen växte med {tillvaxt} jämfört med föregående tolvmånadersperiod."
            if "tillvaxt" in facts
            else ""
        )
        + " Justerad EBITDA uppgår till {justerad_ebitda}.",
        "",
        "## Normaliseringar",
        "",
    ]
    posts = [f"post_{n}" for n in range(1, MAX_POSTS + 1) if f"post_{n}" in facts]
    if posts:
        lines += ["Justeringar med tillräcklig säkerhet, sammantaget {justering}{urval}:", ""]
        lines += [f"- {{{k}}}" for k in posts]
    else:
        lines.append("Inga justeringar har gjorts med tillräcklig säkerhet.")
    candidates = [k for k in ("kandidat_1", "kandidat_2") if k in facts]
    if candidates:
        lines += ["", "Ej medräknat, att bedöma:", ""]
        lines += [f"- {{{k}}}" for k in candidates]
    lines += [
        "",
        "## Rörelsekapital och nettoskuld",
        "",
        "Rörelsekapitalet uppgick till {rorelsekapital} vid periodens slut"
        + (
            ", med en normalnivå om {rorelsekapital_normal}"
            if "rorelsekapital_normal" in facts
            else ""
        )
        + ". Per {nettoskuld_datum} har bolaget {nettoskuld}.",
        "",
        "## Underlagets kvalitet",
        "",
        "Avstämningen av bokföringen visar {avstamning}. Av {antal_flaggor} flaggade poster kräver "
        "{antal_granskning} granskning, och {antal_fragor} frågor har tagits fram.",
        "",
        "## Frågor till säljaren",
        "",
    ]
    lines += [f"- {{{k}}}" for k in ("fraga_1", "fraga_2", "fraga_3") if k in facts]
    return _fill("\n".join(lines), facts)


def _check(facts: dict[str, str]) -> Callable[[_Memo], list[str]]:
    def check(parsed: _Memo) -> list[str]:
        text = parsed.markdown
        problems: list[str] = []
        if unknown := sorted({k for k in _PLACEHOLDER.findall(text) if k not in facts}):
            problems.append(
                f"Okända nycklar: {', '.join(unknown)}. Använd bara underlagets nycklar."
            )
        prose = _LIST_NUMBER.sub("", _PLACEHOLDER.sub("", text))
        if re.search(r"\d", prose):
            problems.append("Texten innehåller siffror. Skriv alla tal som platshållare.")
        if _DASH.search(_PLACEHOLDER.sub("X", text)):
            problems.append("Texten använder tankstreck som skiljetecken.")
        if "## Sammanfattning" not in text:
            problems.append("Rubriken ## Sammanfattning saknas.")
        if not unknown and len(_fill(text, facts).split()) > MAX_WORDS:
            problems.append("Memot är för långt. Högst en sida.")
        return problems

    return check


def write_memo(
    result: AnalysisResult,
    client: anthropic.Anthropic | None,
    model: str = DEFAULT_MODEL,
) -> str:
    """Swedish markdown memo. Raises LlmError when the API rejects the configuration."""
    facts = memo_facts(result)
    if client is None:
        return _template(facts)
    content = "Underlag, nyckel och värde:\n" + json.dumps(facts, ensure_ascii=False, indent=1)
    try:
        parsed = ask_structured(
            client,
            model=model,
            system=[{"type": "text", "text": SYSTEM_PROMPT}],
            content=content,
            output_type=_Memo,
            check=_check(facts),
            usage=Usage(),
        )
    except LlmUnavailable as exc:
        log.warning("LLM-anrop misslyckades efter SDK:ns omförsök: %s", exc)
        parsed = None
    return _template(facts) if parsed is None else _fill(parsed.markdown, facts)

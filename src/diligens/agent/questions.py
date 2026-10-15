"""DD questions to the seller, one per flagged category and account.

Code groups the flags and fixes account, vouchers and amount. Claude only writes the wording
of the question, without digits; the reference with account, vouchers and amount is appended
by code. Without a client, or when the model fails, Swedish templates are used.
"""

import json
import logging
import re
from collections import defaultdict
from decimal import Decimal

import anthropic
import pydantic
from anthropic.types import OutputConfigParam
from pydantic import BaseModel

from diligens.config import DEFAULT_MODEL
from diligens.formatting import format_tsek
from diligens.models import DDQuestion, Flag, FlagCategory, Ledger

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_TOKENS = 16000
OUTPUT_CONFIG: OutputConfigParam = {"effort": "medium"}
ZERO = Decimal("0")

ORDER: tuple[FlagCategory, ...] = (
    "periodisering",
    "agarrelaterad",
    "narstaende",
    "engangspost",
    "avvikelse",
)
TEMPLATES: dict[FlagCategory, str] = {
    "periodisering": (
        "Vilken period avser posterna på {name}, och när levererades varan eller tjänsten?"
    ),
    "agarrelaterad": (
        "Vilken del av kostnaderna på {name} avser ägaren eller ägarens bolag, och vad vore en "
        "marknadsmässig ersättning för motsvarande insats?"
    ),
    "narstaende": (
        "Vilka villkor gäller för transaktionerna med närstående på {name}, och hur förhåller "
        "de sig till marknadsmässiga villkor?"
    ),
    "engangspost": (
        "Kan ni beskriva bakgrunden till posterna på {name} och bekräfta att de inte återkommer?"
    ),
    "avvikelse": "Vad förklarar mönstret på {name} i de markerade verifikationerna?",
}

SYSTEM_PROMPT = """\
Du biträder en rådgivare i en finansiell due diligence av ett svenskt ägarlett bolag. För
varje grupp av flaggade bokföringsposter skriver du en kort och professionell fråga till
säljaren på svenska. Frågan ska vara neutral, inte anklagande, och be om det underlag som
behövs för att bedöma posten. Grupperna har kategori, konto och exempel på verifikationstexter.

Skriv inga siffror alls i frågan, eftersom koden lägger till konto, verifikationer och belopp
efteråt. Använd inte tankstreck som skiljetecken. Avsluta varje fråga med frågetecken. Svara
med exakt en fråga per grupp och samma id som i underlaget.
"""


class _LlmQuestion(BaseModel):
    id: str
    text: str


class _LlmQuestions(BaseModel):
    questions: list[_LlmQuestion]


class _Group(BaseModel):
    id: str
    category: FlagCategory
    account: str
    name: str
    flags: list[Flag]

    @property
    def amount(self) -> Decimal:
        return sum((f.amount for f in self.flags), ZERO)


def _groups(flags: list[Flag], ledger: Ledger) -> list[_Group]:
    grouped: dict[tuple[FlagCategory, str], list[Flag]] = defaultdict(list)
    for flag in flags:
        if flag.category != "normal":
            grouped[(flag.category, flag.account)].append(flag)
    keys = sorted(
        grouped,
        key=lambda k: (ORDER.index(k[0]), -abs(sum((f.amount for f in grouped[k]), ZERO))),
    )
    return [
        _Group(
            id=f"g{i}",
            category=category,
            account=account,
            name=ledger.accounts[account].name if account in ledger.accounts else account,
            flags=grouped[(category, account)],
        )
        for i, (category, account) in enumerate(keys, start=1)
    ]


def _reference(group: _Group) -> str:
    refs = [f.voucher_ref for f in group.flags]
    shown = ", ".join(refs[:5]) + (f" m.fl. ({len(refs)} st)" if len(refs) > 5 else "")
    return (
        f" (konto {group.account} {group.name}, verifikat {shown}, "
        f"{format_tsek(abs(group.amount))} TSEK)"
    )


def _request(groups: list[_Group], ledger: Ledger) -> str:
    vouchers = {v.ref: v for v in ledger.vouchers}
    lines = []
    for g in groups:
        texts = list(dict.fromkeys(vouchers[f.voucher_ref].text for f in g.flags))[:5]
        lines.append(
            json.dumps(
                {
                    "id": g.id,
                    "kategori": g.category,
                    "konto": g.name,
                    "antal_verifikat": len(g.flags),
                    "verifikationstexter": texts,
                    "motiveringar": list(dict.fromkeys(f.rationale for f in g.flags))[:3],
                },
                ensure_ascii=False,
            )
        )
    return "Skriv en fråga per grupp:\n" + "\n".join(lines)


def _problems(parsed: _LlmQuestions, ids: list[str]) -> list[str]:
    seen = [q.id for q in parsed.questions]
    problems = []
    if missing := [i for i in ids if i not in seen]:
        problems.append(f"Fråga saknas för: {', '.join(missing)}.")
    if extra := sorted({i for i in seen if i not in ids or seen.count(i) > 1}):
        problems.append(f"Okända eller dubblerade id: {', '.join(extra)}.")
    if digits := [q.id for q in parsed.questions if re.search(r"\d", q.text)]:
        problems.append(f"Frågan innehåller siffror för: {', '.join(digits)}. Skriv utan siffror.")
    if bad := [q.id for q in parsed.questions if not q.text.strip().endswith("?")]:
        problems.append(f"Frågan avslutas inte med frågetecken för: {', '.join(bad)}.")
    return problems


def _ask(
    groups: list[_Group], ledger: Ledger, client: anthropic.Anthropic, model: str
) -> dict[str, str]:
    """Question wording per group id from Claude; empty if it never gets it right."""
    request = _request(groups, ledger)
    ids = [g.id for g in groups]
    feedback = ""
    for _attempt in range(MAX_ATTEMPTS):
        content = request + (f"\n\nFöregående svar var ogiltigt. {feedback}" if feedback else "")
        try:
            response = client.messages.parse(
                model=model,
                max_tokens=MAX_TOKENS,
                system=[
                    {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": content}],
                output_format=_LlmQuestions,
                output_config=OUTPUT_CONFIG,
            )
        except pydantic.ValidationError as exc:
            feedback = f"Svaret följde inte schemat: {exc.error_count()} fel."
            continue
        except (
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ) as exc:
            log.warning("LLM-anrop misslyckades efter SDK:ns omförsök: %s", type(exc).__name__)
            return {}
        if response.stop_reason != "end_turn" or response.parsed_output is None:
            feedback = f"Svaret avbröts ({response.stop_reason})."
            continue
        problems = _problems(response.parsed_output, ids)
        if not problems:
            return {q.id: q.text.strip() for q in response.parsed_output.questions}
        feedback = " ".join(problems)
    return {}


def dd_questions(
    flags: list[Flag],
    ledger: Ledger,
    client: anthropic.Anthropic | None,
    model: str = DEFAULT_MODEL,
) -> list[DDQuestion]:
    """One question per (category, account) among non-normal flags, most material first."""
    groups = _groups(flags, ledger)
    if not groups:
        return []
    wording = _ask(groups, ledger, client, model) if client is not None else {}
    return [
        DDQuestion(
            text=wording.get(g.id, TEMPLATES[g.category].format(name=g.name.lower()))
            + _reference(g),
            voucher_refs=[f.voucher_ref for f in g.flags],
            accounts=[g.account],
            amount=g.amount,
        )
        for g in groups
    ]

"""DD questions to the seller, one per flagged category and account.

Code groups the flags and fixes account, vouchers and amount. Claude only writes the wording
of the question, without digits; the reference with account, vouchers and amount is appended
by code. Without a client, or when the model fails, Swedish templates are used.
"""

import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal

import anthropic
from pydantic import BaseModel, Field

from diligens.agent._llm import LlmUnavailable, Usage, ask_structured
from diligens.analysis.bridge import ebitda_effect
from diligens.analysis.statements import month_key
from diligens.config import DEFAULT_MODEL
from diligens.formatting import format_tsek
from diligens.models import DDQuestion, Flag, FlagCategory, Ledger

log = logging.getLogger(__name__)

ZERO = Decimal("0")
_DASH = re.compile(r"\s[-–—]\s")

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
    amount: Decimal = Field(description="net EBITDA effect of the vouchers, raw sign")
    first_month: str
    last_month: str


def _groups(flags: list[Flag], ledger: Ledger) -> list[_Group]:
    vouchers = {v.ref: v for v in ledger.vouchers}
    grouped: dict[tuple[FlagCategory, str], list[Flag]] = defaultdict(list)
    for flag in flags:
        if flag.category != "normal":
            grouped[(flag.category, flag.account)].append(flag)

    def amount(key: tuple[FlagCategory, str]) -> Decimal:
        return sum((ebitda_effect(vouchers[f.voucher_ref]) for f in grouped[key]), ZERO)

    keys = sorted(grouped, key=lambda k: (ORDER.index(k[0]), -abs(amount(k))))
    groups = []
    for i, (category, account) in enumerate(keys, start=1):
        members = grouped[(category, account)]
        months = sorted(month_key(vouchers[f.voucher_ref].date) for f in members)
        groups.append(
            _Group(
                id=f"g{i}",
                category=category,
                account=account,
                name=ledger.accounts[account].name if account in ledger.accounts else account,
                flags=members,
                amount=amount((category, account)),
                first_month=months[0],
                last_month=months[-1],
            )
        )
    return groups


def _reference(group: _Group) -> str:
    refs = [f.voucher_ref for f in group.flags]
    shown = ", ".join(refs[:5]) + (f" m.fl. ({len(refs)} st)" if len(refs) > 5 else "")
    period = (
        group.first_month
        if group.first_month == group.last_month
        else f"{group.first_month} till {group.last_month}"
    )
    return (
        f" (konto {group.account} {group.name}, verifikat {shown}, {period}, "
        f"{format_tsek(abs(group.amount))} TSEK)"
    )


def _without_digits(text: str) -> str:
    return " ".join(re.sub(r"\d+\s*%?", " ", text).split())


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
                    "konto": _without_digits(g.name),
                    "antal_verifikat": len(g.flags),
                    "verifikationstexter": texts,
                    "motiveringar": list(dict.fromkeys(f.rationale for f in g.flags))[:3],
                },
                ensure_ascii=False,
            )
        )
    return "Skriv en fråga per grupp:\n" + "\n".join(lines)


def _check(ids: list[str]) -> Callable[[_LlmQuestions], list[str]]:
    def check(parsed: _LlmQuestions) -> list[str]:
        seen = [q.id for q in parsed.questions]
        problems = []
        if missing := [i for i in ids if i not in seen]:
            problems.append(f"Fråga saknas för: {', '.join(missing)}.")
        if extra := sorted({i for i in seen if i not in ids or seen.count(i) > 1}):
            problems.append(f"Okända eller dubblerade id: {', '.join(extra)}.")
        if digits := [q.id for q in parsed.questions if re.search(r"\d", q.text)]:
            problems.append(
                f"Frågan innehåller siffror för: {', '.join(digits)}. Skriv utan siffror."
            )
        if dashes := [q.id for q in parsed.questions if _DASH.search(q.text)]:
            problems.append(f"Frågan använder tankstreck för: {', '.join(dashes)}.")
        if bad := [q.id for q in parsed.questions if not q.text.strip().endswith("?")]:
            problems.append(f"Frågan avslutas inte med frågetecken för: {', '.join(bad)}.")
        return problems

    return check


def dd_questions(
    flags: list[Flag],
    ledger: Ledger,
    client: anthropic.Anthropic | None,
    model: str = DEFAULT_MODEL,
) -> list[DDQuestion]:
    """One question per (category, account) among non-normal flags, most material first.

    Raises LlmError when the API rejects the configuration; falls back to templates when the
    API is unavailable or the model never gets the format right.
    """
    groups = _groups(flags, ledger)
    if not groups:
        return []
    wording: dict[str, str] = {}
    if client is not None:
        try:
            parsed = ask_structured(
                client,
                model=model,
                system=[{"type": "text", "text": SYSTEM_PROMPT}],
                content=_request(groups, ledger),
                output_type=_LlmQuestions,
                check=_check([g.id for g in groups]),
                usage=Usage(),
            )
        except LlmUnavailable as exc:
            log.warning("LLM-anrop misslyckades efter SDK:ns omförsök: %s", exc)
            parsed = None
        if parsed is not None:
            wording = {q.id: q.text.strip() for q in parsed.questions}
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

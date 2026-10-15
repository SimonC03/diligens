"""Classify prefiltered vouchers into normalization categories with Claude.

The model only chooses category, rationale and confidence. Account and amount on each Flag
come from the voucher, so no number in the output is produced by the model. Output is
schema constrained (structured outputs) and then checked against the batch: every voucher
exactly once, no unknown refs, no digits in the rationale. Failures are fed back and
retried; a batch that still fails becomes "avvikelse" with confidence 0, i.e. review.
"""

import json
import logging
import re
from collections.abc import Iterator
from decimal import Decimal

import anthropic
import pydantic
from anthropic.types import OutputConfigParam, TextBlockParam
from pydantic import BaseModel, Field

from diligens.config import DEFAULT_MODEL
from diligens.models import Flag, FlagCategory, FlaggingResult, Ledger, PrefilterHit, Voucher

log = logging.getLogger(__name__)

BATCH_SIZE = 50
MAX_ATTEMPTS = 3
MAX_TOKENS = 16000
OUTPUT_CONFIG: OutputConfigParam = {"effort": "medium"}
REVIEW_TEXT = "Kunde inte klassas automatiskt och kräver granskning."

SYSTEM_PROMPT = """\
Du biträder en rådgivare i en finansiell due diligence av ett svenskt ägarlett bolag. Du får
verifikationer ur bolagets bokföring (SIE4, BAS-kontoplan, debet positivt och kredit
negativt) som ett förfilter har valt ut. Bedöm varje verifikation och välj exakt en kategori:

- engangspost: engångskostnad eller engångsintäkt som inte återkommer, till exempel flytt,
  tvist, rekryteringsarvode eller försäkringsersättning.
- agarrelaterad: kostnad som gäller ägaren snarare än verksamheten, till exempel privat bil,
  lön till familj eller arvode till ägarens bolag.
- narstaende: transaktion med närstående part på villkor som kan avvika från marknadens.
- periodisering: intäkt eller kostnad bokförd i fel period, till exempel faktura före
  leverans vid årsskiftet.
- avvikelse: ovanligt mönster som är värt en fråga men inte en normalisering, till exempel en
  topp eller en återföring.
- normal: löpande post utan åtgärd.

Förfiltret är brett, så de flesta verifikationer är normala. Välj normal när inget i text,
konto eller mönster pekar på något annat. Sätt confidence mellan 0 och 1 efter hur säker
bedömningen är utifrån det underlag du ser. Under 0,7 granskas posten manuellt.

Skriv rationale på svenska i en eller två korta meningar. Utgå från verifikationstext, konto
och förfiltrets skäl. Skriv inga siffror alls i rationale, inte heller belopp, datum eller
kontonummer, eftersom koden visar dem från bokföringen. Använd inte tankstreck som skiljetecken.
Hitta inte på fakta som inte syns i underlaget.

Svara med en flagga per verifikation i batchen, med samma ref som i underlaget.
"""


class _LlmFlag(BaseModel):
    voucher_ref: str
    category: FlagCategory
    rationale: str
    confidence: float = Field(ge=0.0, le=1.0)


class _LlmBatch(BaseModel):
    flags: list[_LlmFlag]


def _chart(ledger: Ledger) -> str:
    used = {t.account for v in ledger.vouchers for t in v.transactions}
    lines = [f"{n} {ledger.accounts[n].name}" if n in ledger.accounts else n for n in sorted(used)]
    return "Bolagets kontoplan (konton som används):\n" + "\n".join(lines)


def _main_line(voucher: Voucher) -> tuple[str, Decimal]:
    """The largest EBITDA line (3000-7699, 7900-7999) on the voucher, else the largest line."""

    def in_ebitda(account: str) -> bool:
        return account.isdigit() and (3000 <= int(account) <= 7699 or 7900 <= int(account) <= 7999)

    lines = [t for t in voucher.transactions if in_ebitda(t.account)] or voucher.transactions
    main = max(lines, key=lambda t: abs(t.amount))
    return main.account, main.amount


def _batch_text(batch: list[PrefilterHit], vouchers: dict[str, Voucher], ledger: Ledger) -> str:
    items = []
    for hit in batch:
        v = vouchers[hit.voucher_ref]
        items.append(
            {
                "ref": v.ref,
                "datum": v.date.isoformat(),
                "text": v.text,
                "rader": [
                    {
                        "konto": t.account,
                        "namn": ledger.accounts[t.account].name
                        if t.account in ledger.accounts
                        else "",
                        "belopp": f"{t.amount:.2f}",
                    }
                    for t in v.transactions
                ],
                "forfilter": hit.reasons,
            }
        )
    body = "\n".join(json.dumps(i, ensure_ascii=False) for i in items)
    return f"Bedöm dessa {len(batch)} verifikationer, en per rad:\n{body}"


def _problems(parsed: _LlmBatch, refs: list[str]) -> list[str]:
    problems: list[str] = []
    seen = [f.voucher_ref for f in parsed.flags]
    missing = [r for r in refs if r not in seen]
    unknown = sorted({r for r in seen if r not in refs})
    duplicated = sorted({r for r in seen if seen.count(r) > 1})
    if missing:
        problems.append(f"Flagga saknas för: {', '.join(missing)}.")
    if unknown:
        problems.append(f"Okända ref som inte finns i batchen: {', '.join(unknown)}.")
    if duplicated:
        problems.append(f"Flera flaggor för samma ref: {', '.join(duplicated)}.")
    with_digits = [f.voucher_ref for f in parsed.flags if re.search(r"\d", f.rationale)]
    if with_digits:
        problems.append(
            f"Rationale innehåller siffror för: {', '.join(with_digits)}. Skriv om utan siffror."
        )
    return problems


def _batches(hits: list[PrefilterHit]) -> Iterator[list[PrefilterHit]]:
    for start in range(0, len(hits), BATCH_SIZE):
        yield hits[start : start + BATCH_SIZE]


def _flag(voucher: Voucher, category: FlagCategory, rationale: str, confidence: float) -> Flag:
    account, amount = _main_line(voucher)
    return Flag(
        voucher_ref=voucher.ref,
        account=account,
        amount=amount,
        category=category,
        rationale=rationale,
        confidence=confidence,
    )


def flag_vouchers(
    hits: list[PrefilterHit],
    ledger: Ledger,
    client: anthropic.Anthropic | None,
    model: str = DEFAULT_MODEL,
) -> FlaggingResult:
    """Classify prefilter hits. With `client=None` every hit becomes an "avvikelse" for review."""
    vouchers = {v.ref: v for v in ledger.vouchers}
    if client is None:
        return FlaggingResult(
            flags=[
                _flag(
                    vouchers[h.voucher_ref], "avvikelse", "Förfilter: " + "; ".join(h.reasons), 0.0
                )
                for h in hits
            ]
        )

    system: list[TextBlockParam] = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text", "text": _chart(ledger), "cache_control": {"type": "ephemeral"}},
    ]
    result = FlaggingResult(flags=[])
    for batch in _batches(hits):
        refs = [h.voucher_ref for h in batch]
        request = _batch_text(batch, vouchers, ledger)
        feedback = ""
        parsed: _LlmBatch | None = None
        for _attempt in range(MAX_ATTEMPTS):
            content = request + (
                f"\n\nFöregående svar var ogiltigt. {feedback}" if feedback else ""
            )
            result.calls += 1
            try:
                response = client.messages.parse(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    messages=[{"role": "user", "content": content}],
                    output_format=_LlmBatch,
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
                break
            usage = response.usage
            result.input_tokens += usage.input_tokens
            result.output_tokens += usage.output_tokens
            result.cache_read_tokens += usage.cache_read_input_tokens or 0
            if response.stop_reason != "end_turn" or response.parsed_output is None:
                feedback = f"Svaret avbröts ({response.stop_reason})."
                continue
            problems = _problems(response.parsed_output, refs)
            if not problems:
                parsed = response.parsed_output
                break
            feedback = " ".join(problems)

        if parsed is None:
            result.failed_batches += 1
            result.flags += [_flag(vouchers[r], "avvikelse", REVIEW_TEXT, 0.0) for r in refs]
            continue
        by_ref = {f.voucher_ref: f for f in parsed.flags}
        result.flags += [
            _flag(vouchers[r], by_ref[r].category, by_ref[r].rationale, by_ref[r].confidence)
            for r in refs
        ]
    return result

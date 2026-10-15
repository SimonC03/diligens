"""Classify prefiltered vouchers into normalization categories with Claude.

The model only chooses category, rationale and confidence. Account and amount on each Flag
come from the voucher, so no number in the output is produced by the model. Output is
schema constrained and then checked against the batch: every voucher exactly once, no
unknown refs, no digits or dashes in the rationale. Failures are fed back and retried; a
batch that still fails becomes "avvikelse" with confidence 0, i.e. review.

A recurring series (same account, amount and text every month) is sent once and its
classification applied to every voucher in the series.
"""

import json
import logging
import re
from collections.abc import Callable, Iterator
from decimal import Decimal

import anthropic
from anthropic.types import TextBlockParam
from pydantic import BaseModel, Field

from diligens.agent._llm import LlmUnavailable, Usage, ask_structured
from diligens.analysis.mapping import is_ebitda_account
from diligens.config import DEFAULT_MODEL
from diligens.models import Flag, FlagCategory, FlaggingResult, Ledger, PrefilterHit, Voucher

log = logging.getLogger(__name__)

BATCH_SIZE = 50
REVIEW_TEXT = "Kunde inte klassas automatiskt och kräver granskning."
UNAVAILABLE_TEXT = "API:t svarade inte, posten kräver granskning."

SYSTEM_PROMPT = """\
Du biträder en rådgivare i en finansiell due diligence av ett svenskt ägarlett bolag. Du får
verifikationer ur bolagets bokföring (SIE4, BAS-kontoplan, debet positivt och kredit
negativt) som ett förfilter har valt ut. Bedöm varje verifikation och välj exakt en kategori:

- engangspost: engångskostnad eller engångsintäkt som inte återkommer, till exempel flytt,
  tvist, rekryteringsarvode eller försäkringsersättning.
- agarrelaterad: kostnad som gäller ägaren snarare än verksamheten, till exempel privat bil,
  lön till familj eller arvode till ägarens bolag.
- narstaende: affärstransaktion med närstående part där villkoren kan avvika från marknadens,
  till exempel hyra av lokal från ägaren eller inköp från ett bolag i ägarens sfär.
- periodisering: intäkt eller kostnad bokförd i fel period, till exempel faktura före
  leverans vid årsskiftet.
- avvikelse: ovanligt mönster som är värt en fråga men inte en normalisering, till exempel en
  topp eller en återföring.
- normal: löpande post utan åtgärd.

Välj agarrelaterad när kostnaden gynnar ägaren eller ägarens bolag utan tydlig motprestation
till verksamheten. Välj narstaende när det finns en verklig affär med en närstående part och
frågan gäller om priset är marknadsmässigt. En agarrelaterad, engangspost eller periodisering
justerar EBITDA med verifikationens hela resultateffekt, så var säker innan du väljer dem.

Förfiltret är brett, så de flesta verifikationer är normala. Välj normal när inget i text,
konto eller mönster pekar på något annat. Sätt confidence mellan 0 och 1 efter hur säker
bedömningen är utifrån det underlag du ser. Under 0,7 granskas posten manuellt. En post
markerad som serie gäller alla verifikationer i serien.

Skriv rationale på svenska i en eller två korta meningar. Utgå från verifikationstext, konto
och förfiltrets skäl. Skriv inga siffror alls i rationale, inte heller belopp, datum eller
kontonummer, eftersom koden visar dem från bokföringen. Använd inte tankstreck som skiljetecken.
Hitta inte på fakta som inte syns i underlaget.

Svara med en flagga per verifikation i batchen, med samma ref som i underlaget.
"""

_DASH = re.compile(r"\s[-–—]\s")


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
    """The largest EBITDA line on the voucher, else the largest line."""
    lines = [t for t in voucher.transactions if is_ebitda_account(t.account)]
    main = max(lines or voucher.transactions, key=lambda t: abs(t.amount))
    return main.account, main.amount


def _item(
    hit: PrefilterHit, members: list[str], vouchers: dict[str, Voucher], ledger: Ledger
) -> str:
    v = vouchers[hit.voucher_ref]
    item: dict[str, object] = {
        "ref": v.ref,
        "datum": v.date.isoformat(),
        "text": v.text,
        "rader": [
            {
                "konto": t.account,
                "namn": ledger.accounts[t.account].name if t.account in ledger.accounts else "",
                "belopp": f"{t.amount:.2f}",
            }
            for t in v.transactions
        ],
        "forfilter": hit.reasons,
    }
    if len(members) > 1:
        dates = sorted(vouchers[r].date for r in members)
        item["serie"] = (
            f"{len(members)} verifikat med samma konto, belopp och text, "
            f"{dates[0].isoformat()} till {dates[-1].isoformat()}"
        )
    return json.dumps(item, ensure_ascii=False)


def _check(refs: list[str]) -> Callable[[_LlmBatch], list[str]]:
    def check(parsed: _LlmBatch) -> list[str]:
        problems: list[str] = []
        seen = [f.voucher_ref for f in parsed.flags]
        if missing := [r for r in refs if r not in seen]:
            problems.append(f"Flagga saknas för: {', '.join(missing)}.")
        if unknown := sorted({r for r in seen if r not in refs}):
            problems.append(f"Okända ref som inte finns i batchen: {', '.join(unknown)}.")
        if duplicated := sorted({r for r in seen if seen.count(r) > 1}):
            problems.append(f"Flera flaggor för samma ref: {', '.join(duplicated)}.")
        if digits := [f.voucher_ref for f in parsed.flags if re.search(r"\d", f.rationale)]:
            problems.append(
                f"Rationale innehåller siffror för: {', '.join(digits)}. Skriv om utan siffror."
            )
        if dashes := [f.voucher_ref for f in parsed.flags if _DASH.search(f.rationale)]:
            problems.append(f"Rationale använder tankstreck för: {', '.join(dashes)}.")
        return problems

    return check


def _batches(items: list[PrefilterHit]) -> Iterator[list[PrefilterHit]]:
    for start in range(0, len(items), BATCH_SIZE):
        yield items[start : start + BATCH_SIZE]


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
    """Classify prefilter hits. With `client=None` every hit becomes an "avvikelse" for review.

    Raises LlmError when the API rejects the configuration (key, model, request).
    """
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

    # One representative per recurring series; the rest follow its classification.
    members: dict[str, list[str]] = {}
    representatives: list[PrefilterHit] = []
    by_series: dict[str, str] = {}
    for hit in hits:
        if hit.series and hit.series in by_series:
            members[by_series[hit.series]].append(hit.voucher_ref)
            continue
        if hit.series:
            by_series[hit.series] = hit.voucher_ref
        members[hit.voucher_ref] = [hit.voucher_ref]
        representatives.append(hit)

    system: list[TextBlockParam] = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {"type": "text", "text": _chart(ledger), "cache_control": {"type": "ephemeral"}},
    ]
    usage = Usage()
    decided: dict[str, tuple[FlagCategory, str, float]] = {}
    failed = 0
    error: str | None = None
    for batch in _batches(representatives):
        refs = [h.voucher_ref for h in batch]
        if error is not None:
            failed += 1
            continue
        content = f"Bedöm dessa {len(batch)} verifikationer, en per rad:\n" + "\n".join(
            _item(h, members[h.voucher_ref], vouchers, ledger) for h in batch
        )
        try:
            parsed = ask_structured(
                client,
                model=model,
                system=system,
                content=content,
                output_type=_LlmBatch,
                check=_check(refs),
                usage=usage,
            )
        except LlmUnavailable as exc:
            log.warning("LLM-anrop misslyckades efter SDK:ns omförsök: %s", exc)
            error = f"API:t svarade inte ({exc}); återstående poster kräver granskning."
            failed += 1
            continue
        if parsed is None:
            failed += 1
            continue
        for f in parsed.flags:
            decided[f.voucher_ref] = (f.category, f.rationale, f.confidence)

    flags: list[Flag] = []
    for hit in representatives:
        category, rationale, confidence = decided.get(
            hit.voucher_ref,
            ("avvikelse", UNAVAILABLE_TEXT if error else REVIEW_TEXT, 0.0),
        )
        flags += [
            _flag(vouchers[r], category, rationale, confidence) for r in members[hit.voucher_ref]
        ]
    order = {h.voucher_ref: i for i, h in enumerate(hits)}
    flags.sort(key=lambda f: order[f.voucher_ref])
    return FlaggingResult(
        flags=flags,
        calls=usage.calls,
        failed_batches=failed,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        error=error,
    )

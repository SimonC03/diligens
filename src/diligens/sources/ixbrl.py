"""Read key figures from a Swedish annual report in inline XBRL (K2/K3), see
docs/DATA_SOURCES.md.

Values come from `ix:nonFraction`, honouring `format` (Swedish number formats), `scale` and
`sign`. Periods are recognised from the contexts' dates, not their ids, so `period0` and
`PERIOD0` both work: the current year is the duration that ends last, the previous year the
one ending a year earlier, and balance items use the instants on those dates.
"""

import datetime as dt
import io
import re
import zipfile
from decimal import Decimal, InvalidOperation

from lxml import etree

from diligens.models import AnnualReport, Figure

IX = "http://www.xbrl.org/2013/inlineXBRL"
XBRLI = "http://www.xbrl.org/2003/instance"

# Figure key to taxonomy element (se-gen-base, used by both K2 and K3 reports).
DURATION_ELEMENTS = {
    "nettoomsattning": "Nettoomsattning",
    "rorelseresultat": "Rorelseresultat",
    "avskrivningar": "AvskrivningarNedskrivningarMateriellaImmateriellaAnlaggningstillgangar",
    "resultat_efter_finansiella": "ResultatEfterFinansiellaPoster",
    "arets_resultat": "AretsResultat",
    "medelantal_anstallda": "MedelantaletAnstallda",
}
INSTANT_ELEMENTS = {
    "eget_kapital": "EgetKapital",
    "tillgangar": "Tillgangar",
    "soliditet": "Soliditet",
}


def _number(text: str, number_format: str | None) -> Decimal:
    """Parse the displayed number for the given ixt format (Swedish formats included)."""
    raw = "".join(text.split()).replace(" ", "").replace(" ", "")
    fmt = (number_format or "").split(":")[-1].lower()
    if fmt in ("zerodash", "fixed-zero", "fixedzero") or raw in ("-", "–", ""):
        return Decimal(0)
    if fmt in ("numdotdecimal", "num-dot-decimal"):
        raw = raw.replace(",", "")
    elif fmt in ("numcomma", "numspacecomma", "numcommadecimal", "num-comma-decimal") or (
        "," in raw and "." not in raw
    ):
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Kan inte läsa talet {text!r} ({number_format}).") from exc


def _date(text: str | None) -> dt.date | None:
    return dt.date.fromisoformat(text.strip()[:10]) if text else None


def _contexts(
    root: etree._Element,
) -> dict[str, tuple[dt.date | None, dt.date | None, dt.date | None]]:
    """Context id to (start, end, instant)."""
    out = {}
    for ctx in root.iter(f"{{{XBRLI}}}context"):
        period = ctx.find(f"{{{XBRLI}}}period")
        if period is None:
            continue
        out[ctx.get("id", "")] = (
            _date(period.findtext(f"{{{XBRLI}}}startDate")),
            _date(period.findtext(f"{{{XBRLI}}}endDate")),
            _date(period.findtext(f"{{{XBRLI}}}instant")),
        )
    return out


def _text(element: etree._Element) -> str:
    return "".join(element.itertext())


def parse_ixbrl(document: bytes, document_id: str) -> AnnualReport:
    """Key figures from one iXBRL annual report. Raises ValueError if it has no figures."""
    root = etree.parse(io.BytesIO(document), etree.XMLParser(huge_tree=True)).getroot()
    facts = list(root.iter(f"{{{IX}}}nonFraction"))
    if not facts:
        raise ValueError("Dokumentet är ingen årsredovisning med siffror i iXBRL.")
    contexts = _contexts(root)
    durations = sorted(
        {(s, e) for s, e, _ in contexts.values() if s and e}, key=lambda p: p[1], reverse=True
    )
    if not durations:
        raise ValueError("Årsredovisningen saknar räkenskapsår i iXBRL.")
    current = durations[0]
    previous = next((p for p in durations[1:] if (current[1] - p[1]).days in range(360, 372)), None)

    values: dict[tuple[str, str], tuple[Decimal, str]] = {}
    for fact in facts:
        name = fact.get("name", "").split(":")[-1]
        context = fact.get("contextRef", "")
        if (name, context) in values or fact.get(
            "{http://www.w3.org/2001/XMLSchema-instance}nil"
        ) == "true":
            continue
        value = _number(_text(fact), fact.get("format"))
        value *= Decimal(10) ** int(fact.get("scale", "0") or "0")
        if fact.get("sign") == "-":
            value = -value
        values[(name, context)] = (value, context)

    def find(
        element: str, start: dt.date | None, end: dt.date | None, instant: dt.date | None
    ) -> tuple[Decimal, str] | None:
        for context_id, (s, e, i) in contexts.items():
            matches = (s, e) == (start, end) if instant is None else i == instant
            if matches and (element, context_id) in values:
                return values[(element, context_id)]
        return None

    figures: dict[str, Figure] = {}
    for suffix, period in (("", current), ("_fg", previous)):
        if period is None:
            continue
        for key, element in DURATION_ELEMENTS.items():
            hit = find(element, period[0], period[1], None)
            if hit:
                figures[key + suffix] = Figure(
                    value=hit[0],
                    source_ref=f"iXBRL se-gen-base:{element}, kontext {hit[1]} "
                    f"({period[0]} till {period[1]}), dokument {document_id}",
                )
        for key, element in INSTANT_ELEMENTS.items():
            hit = find(element, None, None, period[1])
            if hit:
                figures[key + suffix] = Figure(
                    value=hit[0],
                    source_ref=f"iXBRL se-gen-base:{element}, kontext {hit[1]} "
                    f"({period[1]}), dokument {document_id}",
                )

    names = {
        el.get("name", "").split(":")[-1]: " ".join(_text(el).split())
        for el in root.iter(f"{{{IX}}}nonNumeric")
    }
    identifier = root.findtext(f".//{{{XBRLI}}}identifier") or ""
    org_nr = re.sub(r"\D", "", names.get("Organisationsnummer", "") or identifier)
    return AnnualReport(
        document_id=document_id,
        org_nr=org_nr,
        company_name=names.get("ForetagetsNamn", ""),
        period_start=current[0],
        period_end=current[1],
        figures=figures,
    )


def read_package(package: bytes, document_id: str) -> AnnualReport:
    """The annual report inside Bolagsverket's zip (which also holds the audit report)."""
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        documents = [
            archive.read(name)
            for name in archive.namelist()
            if name.lower().endswith((".xhtml", ".html", ".htm"))
        ]
    best = max(documents, key=lambda d: d.count(b"nonFraction"), default=b"")
    if not best or b"nonFraction" not in best:
        raise ValueError("Paketet innehåller ingen årsredovisning med siffror i iXBRL.")
    return parse_ixbrl(best, document_id)

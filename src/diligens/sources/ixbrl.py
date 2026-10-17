"""Read key figures from a Swedish annual report in inline XBRL (K2/K3), see
docs/DATA_SOURCES.md.

Values come from `ix:nonFraction`, honouring `format` (Swedish number formats), `scale` and
`sign`. Only contexts without a segment or scenario count, so a note split by business line
is never read as the total. Periods are recognised from the contexts' dates, not their ids:
the current year is the duration that ends last, the previous year the one ending about a
year earlier. A previous year is only used when both years are about twelve months long,
since an extended or short first year makes growth meaningless.

Downloaded documents are parsed without DTDs, entity expansion or network access.
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
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"

YEAR_DAYS = range(350, 381)  # a fiscal year of about twelve months
MAX_MEMBER_BYTES = 50 * 1024 * 1024

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

COMMA_DECIMAL = {"numcomma", "numspacecomma", "numdotcomma", "numcommadecimal", "num-comma-decimal"}
DOT_DECIMAL = {"numdotdecimal", "numcommadot", "numspacedot", "num-dot-decimal", "numdot"}


class NotAnnualReport(ValueError):
    """The document has no numeric facts, e.g. an audit report."""


class ReportUnreadable(ValueError):
    """The document should hold figures but could not be read."""


Period = tuple[dt.date | None, dt.date | None, dt.date | None]


def _number(text: str, number_format: str | None) -> Decimal:
    """Parse the displayed number for the given ixt format (Swedish formats included)."""
    raw = "".join(text.split()).replace(" ", "").replace(" ", "")
    fmt = (number_format or "").split(":")[-1].lower()
    if fmt in ("zerodash", "fixed-zero", "fixedzero") or raw in ("-", "–", ""):
        return Decimal(0)
    if fmt in DOT_DECIMAL:
        raw = raw.replace(",", "")
    elif fmt in COMMA_DECIMAL or ("," in raw and "." not in raw):
        raw = raw.replace(".", "").replace(",", ".")
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ReportUnreadable(f"Kan inte läsa talet {text!r} ({number_format}).") from exc


def _date(text: str | None) -> dt.date | None:
    return dt.date.fromisoformat(text.strip()[:10]) if text else None


def _contexts(root: etree._Element) -> dict[str, Period]:
    """Context id to (start, end, instant), for contexts without segment or scenario."""
    out = {}
    for ctx in root.iter(f"{{{XBRLI}}}context"):
        if ctx.find(f".//{{{XBRLI}}}segment") is not None:
            continue
        if ctx.find(f".//{{{XBRLI}}}scenario") is not None:
            continue
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


def _root(document: bytes) -> etree._Element:
    """Parse safely; retry leniently for HTML entities that XHTML does not declare."""
    for recover in (False, True):
        parser = etree.XMLParser(
            resolve_entities=False, no_network=True, load_dtd=False, recover=recover
        )
        try:
            root = etree.parse(io.BytesIO(document), parser).getroot()
        except etree.XMLSyntaxError:
            continue
        if root is not None:
            return root
    raise ReportUnreadable("Årsredovisningen är inte giltig XHTML och kunde inte läsas.")


def _days(start: dt.date | None, end: dt.date | None) -> int:
    return (end - start).days + 1 if start and end else 0


def parse_ixbrl(document: bytes, document_id: str) -> AnnualReport:
    """Key figures from one iXBRL annual report.

    Raises NotAnnualReport when the document has no numeric facts and ReportUnreadable when
    it cannot be read.
    """
    root = _root(document)
    facts = list(root.iter(f"{{{IX}}}nonFraction"))
    if not facts:
        raise NotAnnualReport("Dokumentet är ingen årsredovisning med siffror i iXBRL.")
    contexts = _contexts(root)
    durations = sorted(
        {(s, e) for s, e, _ in contexts.values() if s and e}, key=lambda p: p[1], reverse=True
    )
    if not durations:
        raise ReportUnreadable("Årsredovisningen saknar räkenskapsår i iXBRL.")
    current = durations[0]
    notes: list[str] = []
    previous = next((p for p in durations[1:] if (current[1] - p[1]).days in range(300, 560)), None)
    if _days(*current) not in YEAR_DAYS:
        notes.append(f"Räkenskapsåret är {_days(*current)} dagar, inte tolv månader")
        previous = None
    elif previous is not None and _days(*previous) not in YEAR_DAYS:
        notes.append(f"Föregående räkenskapsår är {_days(*previous)} dagar och jämförs inte")
        previous = None

    values: dict[tuple[str, str], Decimal] = {}
    for fact in facts:
        name = fact.get("name", "").split(":")[-1]
        context = fact.get("contextRef", "")
        if (name, context) in values or context not in contexts or fact.get(XSI_NIL) == "true":
            continue
        try:
            scale = int(fact.get("scale", "0") or "0")
        except ValueError as exc:
            raise ReportUnreadable(f"Ogiltig skala i {name}.") from exc
        value = _number(_text(fact), fact.get("format")) * Decimal(10) ** scale
        values[(name, context)] = -value if fact.get("sign") == "-" else value

    def find(element: str, wanted: Period) -> tuple[Decimal, str] | None:
        for context_id, period in contexts.items():
            matches = period[2] == wanted[2] if wanted[2] else period[:2] == wanted[:2]
            if matches and (element, context_id) in values:
                return values[(element, context_id)], context_id
        return None

    figures: dict[str, Figure] = {}
    for suffix, period in (("", current), ("_fg", previous)):
        if period is None:
            continue
        for key, element in DURATION_ELEMENTS.items():
            hit = find(element, (period[0], period[1], None))
            if hit:
                figures[key + suffix] = Figure(
                    value=hit[0],
                    source_ref=f"iXBRL se-gen-base:{element}, kontext {hit[1]} "
                    f"({period[0]} till {period[1]}), dokument {document_id}",
                )
        for key, element in INSTANT_ELEMENTS.items():
            hit = find(element, (None, None, period[1]))
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
    assert current[0] is not None and current[1] is not None
    return AnnualReport(
        document_id=document_id,
        org_nr=org_nr,
        company_name=names.get("ForetagetsNamn", ""),
        period_start=current[0],
        period_end=current[1],
        figures=figures,
        notes=notes,
    )


def read_package(package: bytes, document_id: str) -> AnnualReport:
    """The annual report inside Bolagsverket's zip (which also holds the audit report)."""
    try:
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            documents = [
                archive.read(info)
                for info in archive.infolist()
                if info.filename.lower().endswith((".xhtml", ".html", ".htm"))
                and info.file_size <= MAX_MEMBER_BYTES
            ]
    except (zipfile.BadZipFile, OSError) as exc:
        raise ReportUnreadable("Paketet från Bolagsverket är inte en giltig zip-fil.") from exc
    best = max(documents, key=lambda d: d.count(b"nonFraction"), default=b"")
    if not best or b"nonFraction" not in best:
        raise NotAnnualReport("Paketet innehåller ingen årsredovisning med siffror i iXBRL.")
    return parse_ixbrl(best, document_id)

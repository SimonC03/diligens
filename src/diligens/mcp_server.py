"""MCP server exposing the Diligens pipeline as tools.

Run with `uv run diligens-mcp` (stdio). Tools call the same pipeline as the CLI and return
Swedish text for the model to read. Failures come back as readable tool errors, never as
tracebacks. An analysis is cached per file, so follow-up questions do not repeat AI calls.
MCP Python SDK 2.x: the server class is MCPServer (FastMCP was renamed in v2).
"""

import threading
from contextlib import ExitStack
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from diligens.agent._llm import LlmError
from diligens.config import get_settings
from diligens.export.excel import CATEGORY_LABELS, STATUS_ORDER, export_dd_pack, flag_statuses
from diligens.export.longlist import export_long_list
from diligens.formatting import format_tsek
from diligens.models import AnalysisResult, ScreeningCriteria
from diligens.pipeline import run_analysis
from diligens.screening import TOP_DEFAULT, run_screening, with_ranks
from diligens.sources.bolagsverket import BolagsverketError, make_bolagsverket_client
from diligens.sources.scb import ScbError, make_scb_client

mcp = MCPServer(
    "diligens",
    instructions=(
        "Diligens gör om ett svenskt bolags bokföring (SIE4) till underlag för finansiell due "
        "diligence. Alla siffror är beräknade i kod från bokföringen. Börja med analysera_sie "
        "och använd sedan flagga_poster, hamta_nyckeltal och exportera_excel för samma fil."
    ),
)

SIE_SUFFIXES = {".se", ".si", ".sie"}

SieFil = Annotated[
    str,
    Field(description="Sökväg till SIE4-filen (.se), helst hela sökvägen."),
]
MedAi = Annotated[
    bool,
    Field(
        description="Använd AI för att klassa poster och skriva frågor och memo. "
        "Utan AI lämnar inga uppgifter datorn."
    ),
]
Narstaende = Annotated[
    list[str] | None,
    Field(description="Namn på bolag eller personer i ägarens sfär, för förfiltret."),
]


@dataclass(frozen=True)
class _Entry:
    mtime: int
    use_llm: bool
    parties: tuple[str, ...]
    result: AnalysisResult


# One analysis per file. Tools run in worker threads, so the lock makes a second call wait
# for the first instead of starting another (paid) AI run.
_cache: dict[str, _Entry] = {}
_lock = threading.Lock()

NO_KEY_NOTE = "AI-nyckel saknas, analysen kördes utan AI och flaggorna kräver granskning."


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _clean(text: str) -> str:
    return text.strip().strip("\"'")


def _path(sie_fil: str) -> Path:
    path = Path(_clean(sie_fil)).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise ToolError(
            f"Filen {path} finns inte. Ange hela sökvägen till SIE4-filen, till exempel "
            "C:/data/bolaget.se."
        )
    if path.suffix.lower() not in SIE_SUFFIXES:
        raise ToolError(
            f"{path.name} är ingen SIE-fil. Exportera bokföringen som SIE4 (.se) från "
            "bokföringsprogrammet och ange den filen."
        )
    return path.resolve()


def _analysis(
    sie_fil: str, med_ai: bool | None, narstaende: list[str] | None
) -> tuple[AnalysisResult, str]:
    """The analysis for this file, reused while the file is unchanged.

    `med_ai=None` accepts whatever is cached and otherwise runs without AI (for the
    deterministic KPIs). `narstaende=None` accepts the cached names, so follow-up tools see
    the same analysis as the summary. The note is worked out on every call.
    """
    path = _path(sie_fil)
    has_key = get_settings().llm_enabled
    note = NO_KEY_NOTE if med_ai and not has_key else ""
    with _lock:
        mtime = path.stat().st_mtime_ns
        entry = _cache.get(str(path))
        if (
            entry is not None
            and entry.mtime == mtime
            and (med_ai is None or entry.use_llm == (med_ai and has_key))
            and (narstaende is None or entry.parties == tuple(sorted(narstaende)))
        ):
            return entry.result, note
        use_llm = bool(med_ai) and has_key
        parties = tuple(sorted(narstaende or []))
        try:
            result = run_analysis(path, use_llm=use_llm, related_parties=list(parties))
        except (ValueError, OSError, LlmError) as exc:
            raise ToolError(f"Analysen kunde inte köras: {exc}") from exc
        _cache[str(path)] = _Entry(mtime, use_llm, parties, result)
        return result, note


REPO = Path(__file__).resolve().parents[2]


def _output_folder(mapp: str) -> Path:
    """Output folder; inside the repository only output/ (gitignored) is allowed, so a real
    company's pack never ends up in a tracked folder."""
    folder = Path(_clean(mapp)).expanduser()
    if not folder.is_absolute():
        folder = Path.cwd() / folder
    folder = folder.resolve()
    output = REPO / "output"
    if folder.is_relative_to(REPO) and not folder.is_relative_to(output):
        raise ToolError(
            f"{folder} ligger i projektmappen. Skriv till {output} eller till en mapp utanför "
            "projektet."
        )
    return folder


def _tsek(value: Decimal | None) -> str:
    return "-" if value is None else f"{format_tsek(value)} TSEK"


def _percent(value: Decimal | None) -> str:
    return "-" if value is None else f"{value * 100:.1f} %".replace(".", ",")


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


@mcp.tool()
def analysera_sie(
    sie_fil: SieFil,
    med_ai: MedAi = True,
    narstaende: Narstaende = None,
) -> str:
    """Analysera en SIE4-fil: avstämning mot filens saldon, nettoomsättning, EBITDA och
    justerad EBITDA (LTM eller hittills i år), rörelsekapital, nettoskuld, normaliseringar
    och antal poster som kräver granskning. Alla belopp i TSEK, beräknade från bokföringen."""
    result, note = _analysis(sie_fil, med_ai, narstaende)
    kpis, bridge, rec = result.kpis, result.bridge, result.reconciliation
    month = bridge.month
    series = kpis.ltm if bridge.basis == "LTM" else kpis.ytd
    basis = "LTM" if bridge.basis == "LTM" else "hittills i år"
    lines = [
        f"{result.company_name} ({result.org_nr}), {kpis.months[0]} till {kpis.months[-1]}.",
        f"AI: {result.model}" if result.llm_used else "AI: av",
    ]
    if note:
        lines.append(note)
    lines += [
        "Avstämning: OK, ingen differens mot SIE-filens saldon"
        if rec.ok
        else f"Avstämning: AVVIKER, total differens {rec.total_difference} kr",
        "",
        f"Nyckeltal ({basis}, {bridge.period[0]} till {bridge.period[1]}):",
        f"- Nettoomsättning {_tsek(series['nettoomsattning'].values[month])}",
        f"- EBITDA {_tsek(series['ebitda'].values[month])}, marginal "
        f"{_percent(series['ebitda_marginal'].values[month])}",
        f"- Justerad EBITDA {_tsek(bridge.adjusted)}",
        f"- Tillväxt LTM {_percent(kpis.ltm['tillvaxt'].values.get(kpis.months[-1]))}",
        f"- Rörelsekapital {kpis.months[-1]} {_tsek(kpis.nwc.values[kpis.months[-1]])}, "
        f"normalnivå {_tsek(kpis.nwc_normal.value)}",
        f"- Nettoskuld {kpis.net_debt_month} {_tsek(kpis.net_debt)}"
        + (" (nettokassa)" if kpis.net_debt < 0 else ""),
        "",
        "Normaliseringar i justerad EBITDA (EBITDA-effekt):",
    ]
    items = sorted(bridge.items, key=lambda i: -abs(i.amount))
    lines += [f"- {i.label}: {_tsek(i.amount)}" for i in items] or ["- inga"]
    if bridge.candidates:
        lines.append("Att bedöma, ingår inte:")
        lines += [f"- {c.label}: {_tsek(c.amount)}" for c in bridge.candidates]
    lines += [
        "",
        f"{len(result.flags)} poster valdes av förfiltret, {len(bridge.review)} kräver "
        f"granskning, {len(result.questions)} DD-frågor. Använd flagga_poster för detaljer.",
    ]
    return "\n".join(lines)


@mcp.tool()
def flagga_poster(
    sie_fil: SieFil,
    med_ai: MedAi = True,
    status: Annotated[
        Literal["alla", "justerad", "granskning", "ej justerad", "utanför perioden", "normal"],
        Field(description="Visa bara poster med denna status."),
    ] = "alla",
    max_antal: Annotated[
        int, Field(ge=1, le=200, description="Högst så många poster, största först.")
    ] = 20,
    narstaende: Narstaende = None,
) -> str:
    """Lista flaggade poster (normaliseringskandidater) med verifikat, datum, konto, belopp i
    resultatet, kategori, konfidens, status i EBITDA-bryggan och motivering. Sorterat på
    status och belopp."""
    result, note = _analysis(sie_fil, med_ai, narstaende)
    statuses = flag_statuses(result)
    wanted = {
        "justerad": "Justerad",
        "granskning": "Kräver granskning",
        "ej justerad": "Ej justerad, fråga",
        "utanför perioden": "Utanför perioden",
        "normal": "Normal",
    }
    rows = sorted(
        ((f, statuses.get(f.voucher_ref, "Normal")) for f in result.flags),
        key=lambda fs: (STATUS_ORDER.index(fs[1]), -abs(fs[0].amount)),
    )
    if status != "alla":
        rows = [(f, s) for f, s in rows if s == wanted[status]]
    shown = rows[:max_antal]
    lines = [note] if note else []
    lines += [
        f"{len(rows)} poster med status {status}, visar {len(shown)}. Belopp i TSEK, intäkt "
        "positiv och kostnad negativ.",
        "",
        "| Verifikat | Datum | Konto | Belopp | Kategori | Konfidens | Status | Motivering |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for flag, flag_status in shown:
        confidence = f"{flag.confidence:.2f}".replace(".", ",")
        lines.append(
            f"| {flag.voucher_ref} | {flag.voucher_date or ''} | {flag.account} "
            f"{_cell(flag.account_name)} | {format_tsek(-flag.amount)} | "
            f"{CATEGORY_LABELS[flag.category]} | {confidence} | {flag_status} | "
            f"{_cell(flag.rationale)} |"
        )
    return "\n".join(lines)


@mcp.tool()
def hamta_nyckeltal(
    sie_fil: SieFil,
    manad: Annotated[
        str | None,
        Field(description="Månad som ÅÅÅÅ-MM. Utelämna för den senaste månaden i filen."),
    ] = None,
) -> str:
    """Nyckeltal för en månad: nettoomsättning, bruttoresultat, EBITDA, EBIT och marginaler
    för månaden, hittills i år och LTM, samt rörelsekapital och nettoskuld per post."""
    # KPIs are deterministic: reuse any cached analysis, otherwise run without AI.
    result, note = _analysis(sie_fil, None, None)
    kpis = result.kpis
    month = (manad or kpis.months[-1]).strip()
    if month not in kpis.months:
        raise ToolError(
            f"Månaden {month} finns inte i filen. Välj en månad från {kpis.months[0]} till "
            f"{kpis.months[-1]}, skriven som ÅÅÅÅ-MM."
        )
    lines = [note] if note else []
    lines += [f"Nyckeltal {month} för {result.company_name}, TSEK.", ""]
    lines.append("| Nyckeltal | Månad | Hittills i år | LTM |")
    lines.append("|---|---|---|---|")
    for key, label in (
        ("nettoomsattning", "Nettoomsättning"),
        ("bruttoresultat", "Bruttoresultat"),
        ("ebitda", "EBITDA"),
        ("ebit", "EBIT"),
    ):
        lines.append(
            f"| {label} | {_tsek(kpis.monthly[key].values[month])} | "
            f"{_tsek(kpis.ytd[key].values.get(month))} | {_tsek(kpis.ltm[key].values[month])} |"
        )
    for key, label in (
        ("bruttomarginal", "Bruttomarginal"),
        ("ebitda_marginal", "EBITDA-marginal"),
    ):
        lines.append(
            f"| {label} | {_percent(kpis.monthly[key].values[month])} | "
            f"{_percent(kpis.ytd[key].values.get(month))} | "
            f"{_percent(kpis.ltm[key].values[month])} |"
        )
    lines += [
        "",
        f"Tillväxt LTM: {_percent(kpis.ltm['tillvaxt'].values[month])}",
        f"Rörelsekapital {month}: {_tsek(kpis.nwc.values[month])}, normalnivå (snitt 12 månader "
        f"till {kpis.months[-1]}): {_tsek(kpis.nwc_normal.value)}",
        "",
        f"Nettoskuld per {kpis.net_debt_month} (positivt ökar skulden):",
    ]
    lines += [f"- {i.label}: {_tsek(i.amount)}" for i in kpis.net_debt_items]
    lines.append(f"- Summa: {_tsek(kpis.net_debt)}")
    if kpis.debt_like_items:
        lines.append("Att bedöma, ingår inte:")
        lines += [f"- {i.label}: {_tsek(i.amount)}" for i in kpis.debt_like_items]
    return "\n".join(lines)


@mcp.tool()
def exportera_excel(
    sie_fil: SieFil,
    mapp: Annotated[str, Field(description="Mapp där dd_pack.xlsx och memo.md skrivs.")] = "output",
    med_ai: MedAi = True,
    narstaende: Narstaende = None,
) -> str:
    """Skapa DD-underlaget i Excel (tio flikar med formler och källor) och ett memo på en sida.
    Returnerar sökvägarna och memot."""
    result, note = _analysis(sie_fil, med_ai, narstaende)
    folder = _output_folder(mapp)
    try:
        workbook = export_dd_pack(result, folder)
        memo = folder / "memo.md"
        memo.write_text(result.memo, encoding="utf-8")
    except PermissionError as exc:
        raise ToolError(
            f"Kunde inte skriva till {folder}. Filen är troligen öppen i Excel; stäng den och "
            "försök igen."
        ) from exc
    except OSError as exc:
        raise ToolError(f"Kunde inte skriva till {folder} ({exc.strerror}).") from exc
    lines = [note] if note else []
    lines += [f"Excel: {workbook}", f"Memo: {memo}", "", result.memo]
    return "\n".join(lines)


@mcp.tool()
def screena_bolag(
    sni: Annotated[
        list[str], Field(min_length=1, description="SNI 2025-koder, fem siffror, t.ex. 62100.")
    ],
    oms_kl: Annotated[
        list[int], Field(description="Omsättningsklasser enligt SCB, t.ex. 6 = 20-49 MSEK.")
    ] = [],  # noqa: B006
    anst_kl: Annotated[
        list[int], Field(description="Anställdaklasser enligt SCB, t.ex. 5 = 20-49 anställda.")
    ] = [],  # noqa: B006
    lan: Annotated[list[str], Field(description="Länskoder för säte, t.ex. 01 Stockholm.")] = [],  # noqa: B006
    topp: Annotated[
        int, Field(ge=0, le=100, description="Hämta årsredovisning för så många bästa.")
    ] = TOP_DEFAULT,
    max_rader: Annotated[int, Field(ge=1, le=100, description="Rader i svaret.")] = 15,
    mapp: Annotated[
        str | None, Field(description="Skriv även long list till denna mapp (Excel).")
    ] = None,
) -> str:
    """Screena förvärvskandidater (Buy & Build): verksamma aktiebolag med vald primär SNI hos
    SCB, filtrerade på omsättningsklass, anställdaklass och län, med passningspoäng 0-100
    (profil 0-60, nyckeltal ur senaste digitala årsredovisning 0-40) och skäl."""
    criteria = ScreeningCriteria(
        sni=[s.strip() for s in sni],
        turnover_classes=[str(c) for c in oms_kl],
        employee_classes=[str(c) for c in anst_kl],
        counties=[c.strip().zfill(2) for c in lan],
    )
    settings = get_settings()
    try:
        with ExitStack() as clients:
            scb = clients.enter_context(make_scb_client(settings))
            registry = (
                clients.enter_context(make_bolagsverket_client(settings)) if topp > 0 else None
            )
            result = run_screening(criteria, scb, registry, top=topp)
        path = export_long_list(result, _output_folder(mapp)) if mapp else None
    except (ScbError, BolagsverketError) as exc:
        raise ToolError(str(exc)) from exc
    except PermissionError as exc:
        raise ToolError(
            "Kunde inte skriva long list. Stäng filen i Excel och försök igen."
        ) from exc
    missing = sum(
        1 for c in result.candidates if c.financial and c.financial.status == "Saknas digitalt"
    )
    lines = [
        f"{len(result.candidates)} kandidater av {result.units_scanned} bolag med vald SNI. "
        f"Årsredovisning sökt för {result.reports_fetched}, saknas digitalt för {missing}.",
        *result.notes,
        "",
        "| Rang | Poäng | Bolag | Org.nr | Ort | Bokslut | Omsättning TSEK | Tillväxt | "
        "Rörelsemarginal | Skäl |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for rank, c in with_ranks(result.candidates)[:max_rader]:
        f = c.financial
        revenue = _tsek(f.revenue.value).replace(" TSEK", "") if f and f.revenue else "-"
        growth = _percent(f.growth.value) if f and f.growth else "-"
        margin = _percent(f.ebit_margin.value) if f and f.ebit_margin else "-"
        reasons = "; ".join(c.profile_reasons[1:] + (f.reasons if f else []))
        lines.append(
            f"| {rank or '-'} | {c.total} | {_cell(c.unit.name)} | {c.org_nr} | "
            f"{c.unit.post_town.title()} | {f.status if f else 'Ej hämtad'} | {revenue} | "
            f"{growth} | {margin} | {_cell(reasons)} |"
        )
    if path:
        lines += ["", f"Long list: {path}"]
    return "\n".join(lines)


@mcp.tool()
def hamta_bokslut(
    org_nr: Annotated[str, Field(description="Organisationsnummer, t.ex. 556123-4567.")],
) -> str:
    """Nyckeltal ur bolagets senaste digitala årsredovisning hos Bolagsverket (iXBRL):
    nettoomsättning, rörelseresultat, årets resultat, eget kapital, soliditet och anställda,
    för året och föregående år, med källa per siffra. Saknas den digitalt sägs det."""
    try:
        with make_bolagsverket_client(get_settings()) as registry:
            report = registry.latest_report(_clean(org_nr))
    except BolagsverketError as exc:
        raise ToolError(str(exc)) from exc
    if report is None:
        return (
            f"Saknas digitalt: {org_nr} har ingen digital årsredovisning hos Bolagsverket "
            "(troligen inlämnad på papper). Inga siffror gissas."
        )
    rows = [
        ("Nettoomsättning", "nettoomsattning", "tsek"),
        ("Rörelseresultat", "rorelseresultat", "tsek"),
        ("Resultat efter finansiella poster", "resultat_efter_finansiella", "tsek"),
        ("Årets resultat", "arets_resultat", "tsek"),
        ("Eget kapital", "eget_kapital", "tsek"),
        ("Summa tillgångar", "tillgangar", "tsek"),
        ("Soliditet", "soliditet", "percent"),
        ("Medelantal anställda", "medelantal_anstallda", "count"),
    ]
    figures = report.figures

    def show(key: str, kind: str) -> str:
        figure = figures.get(key)
        if figure is None or figure.value is None:
            return "-"
        if kind == "tsek":
            return _tsek(figure.value).replace(" TSEK", "")
        if kind == "percent":
            return _percent(figure.value)
        return f"{figure.value.normalize():f}"

    name = report.company_name or org_nr
    lines = [
        f"{name}, årsredovisning {report.period_start} till {report.period_end} "
        f"(dokument {report.document_id}). Belopp i TSEK.",
        *report.notes,
        "",
        f"| Post | {report.period_end.year} | Föregående år | Källa |",
        "|---|---|---|---|",
    ]
    for label, key, kind in rows:
        refs = [figures[k].source_ref for k in (key, key + "_fg") if k in figures]
        source = "; ".join(refs)
        lines.append(
            f"| {label} | {show(key, kind)} | {show(key + '_fg', kind)} | {_cell(source)} |"
        )
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

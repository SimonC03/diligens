"""Diligens CLI. Each command maps to a milestone in docs/PLAN.md."""

from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer

from diligens import __version__
from diligens.agent._llm import LlmError
from diligens.formatting import format_tsek
from diligens.models import AnalysisResult
from diligens.pipeline import run_analysis
from diligens.sie.generator import generate_demo_company

app = typer.Typer(
    help="Diligens: SIE4 bokföring till Financial Due Diligence underlag.",
    no_args_is_help=True,
)


def _not_ready(milestone: str) -> None:
    typer.secho(f"Inte implementerat ännu ({milestone}, se docs/PLAN.md).", fg="yellow")
    raise typer.Exit(code=1)


@app.command()
def version() -> None:
    """Visa version."""
    typer.echo(__version__)


@app.command()
def generate(
    out: Annotated[Path, typer.Option(help="Var SIE4 filen ska skrivas.")] = Path(
        "data/demo/demo_bolag.se"
    ),
    seed: Annotated[int, typer.Option(help="Slumpfrö, samma frö ger samma bolag.")] = 42,
) -> None:
    """Skapa ett syntetiskt demobolag med planterade avvikelser (M1)."""
    facit = generate_demo_company(seed, out)
    typer.echo(f"Skrev {out}")
    typer.echo(f"Skrev {out.parent / 'facit.json'} med {len(facit.anomalies)} avvikelser")


@app.command()
def analyze(
    sie_file: Annotated[Path, typer.Argument(help="SIE4 fil att analysera.")],
    out: Annotated[Path, typer.Option(help="Mapp för Excel och memo.")] = Path("output"),
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Kör utan AI, bara beräkningar.")
    ] = False,
    narstaende: Annotated[
        list[str] | None,
        typer.Option(
            "--narstaende",
            help="Namn på bolag eller personer i ägarens sfär, kan anges flera gånger.",
        ),
    ] = None,
) -> None:
    """Analysera en SIE4 fil och skapa DD underlag (M2 till M4).

    Exitkod 0: klart. 1: fel i indata eller AI-konfiguration. 2: avstämningen avviker.
    """
    try:
        result = run_analysis(sie_file, use_llm=not no_llm, related_parties=narstaende or [])
    except (ValueError, OSError) as exc:
        typer.secho(f"Fel: {exc}", fg="red", err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    except LlmError as exc:
        typer.secho(f"AI-fel: {exc}", fg="red", err=True)
        raise typer.Exit(code=EXIT_ERROR) from exc
    _print_summary(result)
    typer.echo(f"\nExcel och memo till {out} kommer i M4.")
    if not result.reconciliation.ok:
        raise typer.Exit(code=EXIT_RECONCILIATION)


EXIT_ERROR = 1
EXIT_RECONCILIATION = 2

CATEGORY_LABELS: dict[str, str] = {
    "engangspost": "engångsposter",
    "agarrelaterad": "ägarrelaterade",
    "narstaende": "närstående",
    "periodisering": "periodiseringar",
    "avvikelse": "avvikelser",
    "normal": "normala",
}


def _tsek(value: Decimal | None) -> str:
    return "-" if value is None else f"{format_tsek(value)} TSEK"


def _share(value: Decimal | None) -> str:
    return "-" if value is None else f"{value * 100:.1f} %".replace(".", ",")


def _print_summary(result: AnalysisResult) -> None:
    kpis, bridge, rec = result.kpis, result.bridge, result.reconciliation
    month = kpis.months[-1]
    basis = "LTM" if bridge.basis == "LTM" else "hittills i år"
    series = kpis.ltm if bridge.basis == "LTM" else kpis.ytd
    ai = f"AI: {result.model}" if result.llm_used else "AI: av (--no-llm)"
    typer.secho(f"{result.company_name} ({result.org_nr})", bold=True)
    typer.echo(f"Källa: {result.source}, {kpis.months[0]} till {month}. {ai}")

    if rec.ok:
        typer.secho("Avstämning: OK, differens 0,00 mot #RES och #UB", fg="green")
    else:
        typer.secho(
            f"Avstämning: AVVIKER, total differens {rec.total_difference}, "
            f"omappade konton {rec.unmapped_accounts or 'inga'}, "
            f"verifikat utanför räkenskapsår {rec.vouchers_outside_years or 'inga'}",
            fg="red",
        )
    if result.integrity_issues:
        typer.secho(f"Integritetsfel i SIE-filen: {len(result.integrity_issues)}", fg="yellow")

    typer.secho(f"\nNyckeltal {month} ({basis})", bold=True)
    label = "LTM" if bridge.basis == "LTM" else "YTD"
    typer.echo(f"  Nettoomsättning {label}  {_tsek(series['nettoomsattning'].values[month])}")
    typer.echo(f"  EBITDA {label}           {_tsek(series['ebitda'].values[month])}")
    typer.echo(f"  EBITDA-marginal       {_share(series['ebitda_marginal'].values[month])}")
    typer.echo(f"  Tillväxt LTM          {_share(kpis.ltm['tillvaxt'].values[month])}")
    typer.echo(f"  Rörelsekapital        {_tsek(kpis.nwc.values[month])}")
    net_cash = " (nettokassa)" if kpis.net_debt < 0 else ""
    typer.echo(f"  Nettoskuld            {_tsek(kpis.net_debt)}{net_cash}")

    typer.secho(f"\nEBITDA-brygga ({basis}, {bridge.period[0]} till {bridge.period[1]})", bold=True)
    typer.echo(f"  Rapporterad EBITDA    {_tsek(bridge.reported)}")
    for item in bridge.items:
        typer.echo(f"  {item.label:<40} {_tsek(item.amount)}")
    typer.secho(f"  Justerad EBITDA       {_tsek(bridge.adjusted)}", bold=True)

    counts = Counter(f.category for f in result.flags)
    typer.secho("\nFlaggade poster", bold=True)
    typer.echo(
        f"  Förfiltret valde {len(result.prefilter_hits)} verifikat. "
        + ", ".join(f"{CATEGORY_LABELS[cat]} {n}" for cat, n in sorted(counts.items()))
    )
    typer.echo(f"  Kräver granskning: {len(bridge.review)} poster")
    if bridge.outside_period:
        typer.echo(f"  Utanför perioden, ej justerade: {len(bridge.outside_period)} poster")
    if result.flagging.error:
        typer.secho(f"  {result.flagging.error}", fg="yellow")
    elif result.flagging.failed_batches:
        typer.secho(
            f"  {result.flagging.failed_batches} batchar kunde inte klassas, posterna ligger "
            "under Kräver granskning.",
            fg="yellow",
        )

    typer.secho(f"\nDD-frågor ({len(result.questions)})", bold=True)
    for i, question in enumerate(result.questions, start=1):
        typer.echo(f"  {i}. {question.text}")


@app.command()
def screen(
    sni: Annotated[list[str], typer.Option(help="SNI kod, kan anges flera gånger.")],
    oms_kl: Annotated[
        list[int] | None, typer.Option(help="Omsättningsklass enligt SCB:s kodtabell.")
    ] = None,
    anst_kl: Annotated[
        list[int] | None, typer.Option(help="Anställdaklass enligt SCB:s kodtabell.")
    ] = None,
    lan: Annotated[list[str] | None, typer.Option(help="Länskod för säte, valfri.")] = None,
    out: Annotated[Path, typer.Option(help="Mapp för long list.")] = Path("output"),
) -> None:
    """Screena förvärvskandidater via SCB och Bolagsverket (M6)."""
    _not_ready("M6")


if __name__ == "__main__":
    app()

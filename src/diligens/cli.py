"""Diligens CLI. Each command maps to a milestone in docs/PLAN.md."""

from pathlib import Path
from typing import Annotated

import typer

from diligens import __version__

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
    _not_ready("M1")


@app.command()
def analyze(
    sie_file: Annotated[Path, typer.Argument(help="SIE4 fil att analysera.")],
    out: Annotated[Path, typer.Option(help="Mapp för Excel och memo.")] = Path("output"),
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Kör utan AI, bara beräkningar.")
    ] = False,
) -> None:
    """Analysera en SIE4 fil och skapa DD underlag (M2 till M4)."""
    _not_ready("M2")


@app.command()
def screen(
    sni: Annotated[str, typer.Option(help="SNI kod, t.ex. 43210.")],
    size: Annotated[str, typer.Option(help="Storleksklass anställda, t.ex. 5-49.")] = "5-49",
    county: Annotated[str | None, typer.Option(help="Länskod, valfri.")] = None,
    out: Annotated[Path, typer.Option(help="Mapp för long list.")] = Path("output"),
) -> None:
    """Screena förvärvskandidater via SCB och Bolagsverket (M6)."""
    _not_ready("M6")


if __name__ == "__main__":
    app()

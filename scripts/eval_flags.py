"""Evaluate AI flagging on the synthetic demo company against its facit.

    uv run python scripts/eval_flags.py            # live, calls the Anthropic API
    uv run python scripts/eval_flags.py --no-llm   # plumbing only, finds nothing

Exits 0 when at least 5 of the 6 planted anomalies are found (docs/PLAN.md, M3).
"""

import argparse
import sys
import tempfile
from pathlib import Path

from diligens.evaluation import score_flags
from diligens.pipeline import run_analysis
from diligens.sie.generator import generate_demo_company

REQUIRED_FOUND = 5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--out", type=Path, default=None, help="folder for the demo files")
    args = parser.parse_args(argv)

    out = args.out or Path(tempfile.mkdtemp(prefix="diligens-eval-"))
    sie = out / "demo_bolag.se"
    facit = generate_demo_company(args.seed, sie)
    result = run_analysis(sie, use_llm=not args.no_llm)
    report = score_flags(facit, result.flags, result.prefilter_hits)

    print(f"Modell: {result.model or 'ingen (--no-llm)'}   Seed: {args.seed}")
    print(f"{'#':>2}  {'Hittad':<6} {'Rätt':>9} {'Förfilter':>10}  Kategorier  Beskrivning")
    for row in report.rows:
        print(
            f"{row.id:>2}  {'ja' if row.found else 'NEJ':<6} "
            f"{row.correct_vouchers:>4}/{row.total_vouchers:<4} "
            f"{row.in_prefilter:>5}/{row.total_vouchers:<4}  "
            f"{','.join(row.categories):<10}  {row.description}"
        )
    precision = "-" if report.precision is None else f"{report.precision:.0%}"
    print(
        f"\nRecall: {report.found} av {report.total}   Precision: {precision} "
        f"({report.confident_flags} flaggor med konfidens >= 0,7)"
    )
    f = result.flagging
    print(
        f"Anrop: {f.calls}, misslyckade batchar: {f.failed_batches}, tokens in/ut/cache: "
        f"{f.input_tokens}/{f.output_tokens}/{f.cache_read_tokens}"
    )
    return 0 if report.found >= REQUIRED_FOUND else 1


if __name__ == "__main__":
    sys.exit(main())

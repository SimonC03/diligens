"""SIE4 parser. Implemented in M1, see docs/PLAN.md and docs/DOMAIN.md section 1."""

from pathlib import Path

from diligens.models import IntegrityIssue, Ledger


def parse_sie(path: Path) -> Ledger:
    """Parse a SIE4 file (CP437) into a Ledger. Unknown tags become warnings."""
    raise NotImplementedError("M1: parse_sie")


def check_integrity(ledger: Ledger) -> list[IntegrityIssue]:
    """Return every integrity violation from DOMAIN.md section 1. Never raises."""
    raise NotImplementedError("M1: check_integrity")

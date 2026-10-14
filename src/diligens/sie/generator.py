"""Synthetic demo company with planted anomalies. Implemented in M1.

See docs/DOMAIN.md section 6 for the anomalies the generator must plant.
"""

from pathlib import Path

from diligens.models import Facit


def generate_demo_company(seed: int, out: Path) -> Facit:
    """Write a SIE4 file to `out` and return the facit of planted anomalies."""
    raise NotImplementedError("M1: generate_demo_company")

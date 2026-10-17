"""Live checks against SCB and Bolagsverket. Run with: uv run pytest -m live"""

import pytest

from diligens.config import Settings
from diligens.sources.bolagsverket import make_bolagsverket_client
from diligens.sources.scb import make_scb_client

pytestmark = pytest.mark.live


def test_live_scb() -> None:
    settings = Settings()
    if settings.scb_api_key is None:
        pytest.skip("SCB_API_KEY saknas")
    client = make_scb_client(settings)
    assert client.api_info()["apiNamn"] == "AFR API"
    assert client.count("naringsgren", "62100") > 1000
    assert client.code_table("omsklkoder")["6"].startswith("20 000")


def test_live_bolagsverket() -> None:
    settings = Settings()
    if settings.bolagsverket_client_id is None:
        pytest.skip("Bolagsverket saknas i .env")
    client = make_bolagsverket_client(settings)
    # The spec's own test number: known to Bolagsverket, no annual reports.
    assert client.documents("5299999994") == []
    assert client.latest_report("5299999994") is None

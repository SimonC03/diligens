"""Shared test helpers: made-up SCB units and fake SCB and Bolagsverket APIs.

Organisation numbers here deliberately fail the Luhn check, so none can belong to a real
company.
"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import httpx

from diligens.sources.bolagsverket import BolagsverketClient
from diligens.sources.scb import ScbClient

FIXTURES = Path(__file__).parent / "fixtures"


def _luhn_ok(number: str) -> bool:
    total = 0
    for i, d in enumerate(int(c) for c in number):
        d = d * 2 if i % 2 == 0 else d
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def org(n: int) -> str:
    """A fake ten digit organisation number with a wrong check digit."""
    number = f"55990{n:05d}"
    assert not _luhn_ok(number), f"{number} could be a real company; pick another n"
    return number


def unit(
    n: int,
    *,
    sni: str = "62100",
    anst: str = "5",
    lan: str = "01",
    jur: str = "49",
    stat: str = "1",
) -> dict[str, Any]:
    """A made-up legal unit in SCB's partial format."""
    org_nr = org(n)
    return {
        "peOrgNr": f"16{org_nr}",
        "orgNr": org_nr,
        "namn": f"Testbolag {n} AB",
        "postAdress": {
            "gatuAdress": f"Exempelgatan {n}",
            "coAdress": "",
            "postNr": "11122",
            "postOrt": "STOCKHOLM",
        },
        "primarNaringsgren": {
            "rangordning": 1,
            "naringsgren": sni,
            "andelProcent": 100,
            "avdelningsKod": "K",
        },
        "kommunSate": "0180",
        "lanSate": lan,
        "anstKl": anst,
        "ftgStat": stat,
        "jurform": jur,
        "reklamSparrTyp": 0,
        "telefonSparrTyp": 0,
        "epostSparrTyp": 0,
    }


def full(n: int, oms: str = "6", year: int = 2025) -> dict[str, Any]:
    base = unit(n)
    base.pop("primarNaringsgren")
    return base | {
        "aeAnt": 1,
        "startDat": "2010-01-01T00:00:00Z",
        "regDat": "2010-01-01T00:00:00Z",
        "expImp": "0",
        "tel": "",
        "epost": "",
        "omsAr": year,
        "omsKl": oms,
        "agKat": "41",
        "foretagsnamn": f"Testbolag {n} AB",
        "foretagsnamnAnt": 1,
        "arbGivStat": "1",
        "momsStat": "1",
        "fSkattStat": "1",
        "bolStat": "0",
        "privPubl": "1",
        "sektor": "11000",
        "naringsgrenar": [
            {"rangordning": 1, "naringsgren": "62100", "andelProcent": 100, "avdelningsKod": "K"}
        ],
    }


def fake_scb(units: list[dict[str, Any]], turnover: dict[str, list[str]]) -> ScbClient:
    tables = {
        name: json.loads((FIXTURES / "scb" / f"{name}.json").read_text(encoding="utf-8"))
        for name in ("naringsgrenkoder", "omsklkoder", "anstklkoder", "lankoder")
    }

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v1/kodtabeller/"):
            return httpx.Response(200, json=tables[path.rsplit("/", 1)[-1]])
        if path == "/v1/juridiskaenheter/naringsgren/62100":
            jes = units
        elif path.startswith("/v1/juridiskaenheter/omsattningsklass/"):
            wanted = turnover.get(path.rsplit("/", 1)[-1], [])
            jes = [u for u in units if u["peOrgNr"] in wanted]
        else:
            return httpx.Response(404, json={"status": 404, "detail": "x"})
        return httpx.Response(
            200,
            json={"jes": jes, "pagination": {"nextCursorId": 0, "limit": 5000, "hasMore": False}},
        )

    return ScbClient(
        "k",
        transport=httpx.MockTransport(handle),
        sleep=lambda s: None,
        now=lambda: dt.datetime(2026, 10, 17, 12, 0),
    )


def fake_registry() -> BolagsverketClient:
    package = (FIXTURES / "bolagsverket" / "dokument_exempel.zip").read_bytes()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.url.path.endswith("/isalive"):
            return httpx.Response(200, text="OK")
        if request.url.path.endswith("/dokumentlista"):
            org = json.loads(request.content)["identitetsbeteckning"]
            docs = (
                [
                    {
                        "dokumentId": "d1_paket",
                        "rapporteringsperiodTom": "2025-12-31",
                        "registreringstidpunkt": "2026-06-30",
                    }
                ]
                if org == "5599000001"
                else []
            )
            return httpx.Response(200, json={"dokument": docs})
        return httpx.Response(200, content=package)

    return BolagsverketClient("i", "s", transport=httpx.MockTransport(handle))

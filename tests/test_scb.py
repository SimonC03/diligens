"""M6 SCB client tests against recorded spec and code tables and synthetic responses.

No network: httpx.MockTransport serves made-up companies with the real response shape.
"""

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from helpers import full, unit

from diligens.sources.scb import CLIENT_ENDPOINTS, ScbClient, ScbError

FIXTURES = Path(__file__).parent / "fixtures" / "scb"
KEY = "test-key-not-real"


def problem(status: int, detail: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "application/problem+json"},
        json={
            "type": "https://httpstatuses.com/x",
            "title": "x",
            "status": status,
            "detail": detail,
            "instance": "/v1/x",
        },
    )


class Recorder:
    """Serves responses from a handler and records requests and sleeps."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.clock = 0.0

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.handler(request)

        return httpx.MockTransport(handle)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds

    def client(self, now: dt.datetime = dt.datetime(2026, 10, 17, 12, 0)) -> ScbClient:
        return ScbClient(
            KEY,
            transport=self.transport(),
            sleep=self.sleep,
            clock=lambda: self.clock,
            now=lambda: now,
        )


def test_client_endpoints_exist_in_recorded_spec() -> None:
    spec = json.loads((FIXTURES / "openapi.json").read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]
    for path, schema in CLIENT_ENDPOINTS.items():
        assert path in spec["paths"], path
        assert "get" in spec["paths"][path], path
        if schema:
            assert schema in schemas, schema
    # The fields the client reads exist in the spec's models.
    partial = schemas["jePartialData"]["properties"]
    full_fields = schemas["jeFullData"]["properties"]
    for name in (
        "peOrgNr",
        "orgNr",
        "namn",
        "lanSate",
        "anstKl",
        "ftgStat",
        "jurform",
        "primarNaringsgren",
    ):
        assert name in partial, name
    for name in ("omsKl", "omsAr", "naringsgrenar", "foretagsnamn"):
        assert name in full_fields, name
    pagination = schemas["cursorPaginationResponseData"]["properties"]
    assert {"hasMore", "limit", "nextCursorId"} <= set(pagination)


def test_code_tables_are_recorded_and_parsed() -> None:
    tables = {
        name: json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        for name in (
            "naringsgrenkoder",
            "omsklkoder",
            "anstklkoder",
            "lankoder",
            "jurformkoder",
            "ftgstatkoder",
        )
    }
    oms = {row["kod"]: row["klartext"] for row in tables["omsklkoder"]}
    assert oms[6] == "20 000 - 49 999 tkr" and len(oms) == 13
    assert {row["kod"] for row in tables["ftgstatkoder"]} == {"0", "1", "9"}
    assert any(row["kod"] == "62100" for row in tables["naringsgrenkoder"])

    rec = Recorder(lambda r: httpx.Response(200, json=tables["omsklkoder"]))
    codes = rec.client().code_table("omsklkoder")
    assert codes["6"] == "20 000 - 49 999 tkr"  # keys normalised to strings
    assert rec.requests[0].url.path == "/v1/kodtabeller/omsklkoder"


def test_client_paginates_throttles_retries_and_explains_errors() -> None:
    pages = {
        None: ([unit(1), unit(2)], 11, True),
        "11": ([unit(3), unit(4)], 22, True),
        "22": ([unit(5)], 0, False),
    }

    def paged(request: httpx.Request) -> httpx.Response:
        jes, nxt, more = pages[request.url.params.get("cursorId")]
        return httpx.Response(
            200, json={"jes": jes, "pagination": {"nextCursorId": nxt, "limit": 2, "hasMore": more}}
        )

    rec = Recorder(paged)
    units = list(rec.client().legal_units("naringsgren", "62100", limit=2))
    assert [u.org_nr for u in units] == [f"55990{n:05d}" for n in range(1, 6)]
    assert units[0].primary_sni == "62100" and units[0].employee_class == "5"
    assert all(r.headers["X-API-Key"] == KEY for r in rec.requests)
    assert [r.url.params.get("limit") for r in rec.requests] == ["2", "2", "2"]
    assert rec.requests[0].url.path == "/v1/juridiskaenheter/naringsgren/62100"
    # At most five calls per second: calls are spaced by at least 0.2 s.
    assert len(rec.sleeps) == 2 and all(s == pytest.approx(0.2) for s in rec.sleeps)

    # 429 waits (Retry-After) and retries; a dropped connection is retried too.
    answers: list[Any] = [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.RemoteProtocolError("Server disconnected"),
        httpx.Response(200, json=full(7)),
    ]

    def flaky(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    rec = Recorder(flaky)
    company = rec.client().full("165599000007")
    assert company.turnover_class == "6" and company.turnover_year == 2025
    assert company.name == "Testbolag 7 AB" and len(rec.requests) == 3
    assert 2.0 in rec.sleeps

    # Problem details become Swedish messages; the key never appears in them.
    for status, detail, expected in (
        (400, "Ogiltigt PeOrgNr. Måste vara 12 siffror.", "Ogiltigt PeOrgNr"),
        (401, "Invalid or missing API key.", "SCB_API_KEY"),
        (404, "Not found", "hittades inte"),
    ):
        rec = Recorder(lambda r, s=status, d=detail: problem(s, d))
        with pytest.raises(ScbError, match=expected) as err:
            rec.client().full("165599000007")
        assert KEY not in str(err.value)

    # Persistent server errors give up after a few attempts with a clear message.
    rec = Recorder(lambda r: problem(503, "Service Unavailable"))
    with pytest.raises(ScbError, match="SCB svarar inte"):
        rec.client().full("165599000007")
    assert len(rec.requests) == 3

    # The nightly update window: no call is made between 04:00 and 04:30.
    rec = Recorder(lambda r: httpx.Response(200, json=full(1)))
    with pytest.raises(ScbError, match="04:30"):
        rec.client(now=dt.datetime(2026, 10, 18, 4, 10)).full("165599000001")
    assert rec.requests == []

    # Org numbers are normalised to SCB's twelve digits.
    rec = Recorder(lambda r: httpx.Response(200, json=full(8)))
    rec.client().full("559900-0008")
    assert rec.requests[0].url.path == "/v1/juridiskaenheter/165599000008/full"

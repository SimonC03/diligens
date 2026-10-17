"""M5 MCP server tests, through the SDK's in-memory client. No API calls (med_ai=False)."""

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from helpers import fake_registry, fake_scb
from helpers import unit as scb_unit
from mcp import Client

import diligens.mcp_server as server
from diligens.agent._llm import LlmError
from diligens.config import Settings
from diligens.sie.generator import generate_demo_company

ANALYSIS_TOOLS = {"analysera_sie", "flagga_poster", "hamta_nyckeltal", "exportera_excel"}
TOOLS = ANALYSIS_TOOLS | {"screena_bolag", "hamta_bokslut"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="module")
def sie(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mcp") / "demo_bolag.se"
    generate_demo_company(42, path)
    return path


def _text(result: Any) -> str:
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


@pytest.mark.anyio
async def test_tools_are_listed_with_swedish_descriptions() -> None:
    async with Client(server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    assert set(tools) == TOOLS
    for tool in tools.values():
        assert tool.description and any(c in tool.description for c in "åäö"), tool.name
        properties = tool.input_schema["properties"]
        assert tool.name not in ANALYSIS_TOOLS or "sie_fil" in properties
        assert all(p.get("description") for p in properties.values()), tool.name


@pytest.mark.anyio
async def test_tools_run_end_to_end(sie: Path, tmp_path: Path) -> None:
    server.clear_cache()
    async with Client(server.mcp) as client:
        summary = await client.call_tool("analysera_sie", {"sie_fil": str(sie), "med_ai": False})
        assert not summary.is_error, _text(summary)
        text = _text(summary)
        for expected in ("Demobolaget Konsult AB", "Avstämning: OK", "EBITDA", "Nettoskuld"):
            assert expected in text, expected

        flagged = await client.call_tool(
            "flagga_poster", {"sie_fil": str(sie), "med_ai": False, "max_antal": 5}
        )
        rows = [line for line in _text(flagged).splitlines() if line.startswith("|")]
        assert len(rows) == 2 + 5  # header, separator and five items
        assert "Verifikat" in rows[0] and "Konto" in rows[0]

        kpis = await client.call_tool("hamta_nyckeltal", {"sie_fil": str(sie), "manad": "2025-06"})
        assert "2025-06" in _text(kpis) and "EBITDA" in _text(kpis)
        latest = await client.call_tool("hamta_nyckeltal", {"sie_fil": str(sie)})
        assert "Nyckeltal 2025-12" in _text(latest)

        review = await client.call_tool(
            "flagga_poster", {"sie_fil": str(sie), "med_ai": False, "status": "granskning"}
        )
        review_rows = [r for r in _text(review).splitlines() if r.startswith("| ")][1:]
        assert review_rows and all("Kräver granskning" in r for r in review_rows)

        out = tmp_path / "pack"
        export = await client.call_tool(
            "exportera_excel", {"sie_fil": str(sie), "med_ai": False, "mapp": str(out)}
        )
        assert not export.is_error, _text(export)
        assert (out / "dd_pack.xlsx").exists() and (out / "memo.md").exists()
        assert "dd_pack.xlsx" in _text(export)

        # Inside the repository only output/ is allowed, so client data never lands in git.
        docs = await client.call_tool(
            "exportera_excel",
            {"sie_fil": str(sie), "med_ai": False, "mapp": str(server.REPO / "docs")},
        )
        assert docs.is_error and "projektmappen" in _text(docs)


@pytest.mark.anyio
async def test_bad_input_gives_readable_errors(
    sie: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "bokslut.pdf"
    other.write_text("x")
    async with Client(server.mcp) as client:
        missing = await client.call_tool("analysera_sie", {"sie_fil": str(tmp_path / "x.se")})
        assert missing.is_error and "finns inte" in _text(missing)
        quoted = await client.call_tool("analysera_sie", {"sie_fil": f"'{sie}'", "med_ai": False})
        assert not quoted.is_error  # quotes around a pasted path are fine
        wrong = await client.call_tool("analysera_sie", {"sie_fil": str(other)})
        assert wrong.is_error and "SIE" in _text(wrong)
        month = await client.call_tool("hamta_nyckeltal", {"sie_fil": str(sie), "manad": "2031-01"})
        assert month.is_error and "2024-01" in _text(month)  # tells which months exist
        too_many = await client.call_tool(
            "flagga_poster", {"sie_fil": str(sie), "med_ai": False, "max_antal": 0}
        )
        assert too_many.is_error

    def locked(*args: Any, **kwargs: Any) -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(server, "export_dd_pack", locked)
    async with Client(server.mcp) as client:
        blocked = await client.call_tool(
            "exportera_excel", {"sie_fil": str(sie), "med_ai": False, "mapp": str(tmp_path)}
        )
    assert blocked.is_error and "öppen i Excel" in _text(blocked)

    def rejected(*args: Any, **kwargs: Any) -> Any:
        raise LlmError("API-nyckeln avvisades.")

    server.clear_cache()
    monkeypatch.setattr(server, "run_analysis", rejected)
    async with Client(server.mcp) as client:
        failed = await client.call_tool("analysera_sie", {"sie_fil": str(sie), "med_ai": False})
    assert failed.is_error and "API-nyckeln avvisades" in _text(failed)


@pytest.mark.anyio
async def test_no_key_note_and_cache_rules(
    sie: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.clear_cache()
    calls: list[dict[str, Any]] = []
    original = server.run_analysis

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "run_analysis", counting)
    monkeypatch.setattr(
        server, "get_settings", lambda: Settings(_env_file=None, anthropic_api_key=None)
    )
    async with Client(server.mcp) as client:
        # Parallel calls on a cold cache run the analysis once.
        results = await asyncio.gather(
            *(
                client.call_tool(tool, {"sie_fil": str(sie), "med_ai": False})
                for tool in ("analysera_sie", "flagga_poster", "exportera_excel")
            ),
            client.call_tool("hamta_nyckeltal", {"sie_fil": str(sie)}),
        )
        assert not any(r.is_error for r in results[:2] + results[3:])
        assert len(calls) == 1

        # Asking for AI without a key reuses the run without AI and says so every time.
        for _ in range(2):
            again = await client.call_tool("analysera_sie", {"sie_fil": str(sie)})
            assert "AI-nyckel saknas" in _text(again)
        assert len(calls) == 1

        # Related party names change the analysis; follow-ups without names reuse it.
        await client.call_tool(
            "analysera_sie", {"sie_fil": str(sie), "med_ai": False, "narstaende": ["Ägarbolaget"]}
        )
        await client.call_tool("flagga_poster", {"sie_fil": str(sie), "med_ai": False})
        assert len(calls) == 2 and calls[-1]["related_parties"] == ["Ägarbolaget"]

        # A changed file is analysed again.
        copy = tmp_path / "copy.se"
        copy.write_bytes(sie.read_bytes())
        await client.call_tool("hamta_nyckeltal", {"sie_fil": str(copy)})
        os.utime(copy, ns=(copy.stat().st_atime_ns, copy.stat().st_mtime_ns + 10**9))
        await client.call_tool("hamta_nyckeltal", {"sie_fil": str(copy)})
        assert len(calls) == 4


@pytest.mark.anyio
async def test_screening_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    units = [scb_unit(1), scb_unit(2, lan="12"), scb_unit(3, jur="10")]
    turnover = {"6": [u["peOrgNr"] for u in units]}
    monkeypatch.setattr(server, "make_scb_client", lambda s: fake_scb(units, turnover))
    monkeypatch.setattr(server, "make_bolagsverket_client", lambda s: fake_registry())
    async with Client(server.mcp) as client:
        screened = await client.call_tool(
            "screena_bolag",
            {"sni": ["62100"], "oms_kl": [6], "lan": ["1"], "mapp": str(tmp_path / "ll")},
        )
        assert not screened.is_error, _text(screened)
        text = _text(screened)
        assert "2 kandidater av 3 bolag" in text and "Saknas digitalt" in text
        assert "Testbolag 1 AB" in text and (tmp_path / "ll" / "long_list.xlsx").exists()

        report = await client.call_tool("hamta_bokslut", {"org_nr": "559900-0001"})
        assert "Exempelbolaget AB" in _text(report) and "32 450,0" in _text(report)
        assert "se-gen-base:Nettoomsattning" in _text(report)
        missing = await client.call_tool("hamta_bokslut", {"org_nr": "5599000002"})
        assert "Saknas digitalt" in _text(missing)
        empty = await client.call_tool("screena_bolag", {"sni": []})
        assert empty.is_error

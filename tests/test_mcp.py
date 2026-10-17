"""M5 MCP server tests, through the SDK's in-memory client. No API calls (med_ai=False)."""

from pathlib import Path
from typing import Any

import pytest
from mcp import Client

import diligens.mcp_server as server
from diligens.config import Settings
from diligens.sie.generator import generate_demo_company

TOOLS = {"analysera_sie", "flagga_poster", "hamta_nyckeltal", "exportera_excel"}


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
        assert "sie_fil" in properties
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

        kpis = await client.call_tool(
            "hamta_nyckeltal", {"sie_fil": str(sie), "med_ai": False, "manad": "2025-06"}
        )
        assert "2025-06" in _text(kpis) and "EBITDA" in _text(kpis)

        out = tmp_path / "pack"
        export = await client.call_tool(
            "exportera_excel", {"sie_fil": str(sie), "med_ai": False, "mapp": str(out)}
        )
        assert not export.is_error, _text(export)
        assert (out / "dd_pack.xlsx").exists() and (out / "memo.md").exists()
        assert "dd_pack.xlsx" in _text(export)


@pytest.mark.anyio
async def test_bad_input_gives_readable_errors(
    sie: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "bokslut.pdf"
    other.write_text("x")
    async with Client(server.mcp) as client:
        missing = await client.call_tool("analysera_sie", {"sie_fil": str(tmp_path / "x.se")})
        assert missing.is_error and "finns inte" in _text(missing)
        wrong = await client.call_tool("analysera_sie", {"sie_fil": str(other)})
        assert wrong.is_error and "SIE" in _text(wrong)
        month = await client.call_tool(
            "hamta_nyckeltal", {"sie_fil": str(sie), "med_ai": False, "manad": "2031-01"}
        )
        assert month.is_error and "2024-01" in _text(month)  # tells which months exist
        too_many = await client.call_tool(
            "flagga_poster", {"sie_fil": str(sie), "med_ai": False, "max_antal": 0}
        )
        assert too_many.is_error

    # Without an API key the analysis still runs, without AI, and says so.
    server.clear_cache()
    monkeypatch.setattr(
        server, "get_settings", lambda: Settings(_env_file=None, anthropic_api_key=None)
    )
    async with Client(server.mcp) as client:
        offline = await client.call_tool("analysera_sie", {"sie_fil": str(sie)})
    assert not offline.is_error and "utan AI" in _text(offline)


@pytest.mark.anyio
async def test_analysis_is_cached_between_tools(sie: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server.clear_cache()
    calls = 0
    original = server.run_analysis

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "run_analysis", counting)
    async with Client(server.mcp) as client:
        for tool in ("analysera_sie", "flagga_poster", "hamta_nyckeltal"):
            await client.call_tool(tool, {"sie_fil": str(sie), "med_ai": False})
    assert calls == 1

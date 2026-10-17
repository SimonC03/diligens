"""Smoke tests for the scaffold (M0). Milestone tests live in their own files."""

import json
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from diligens import __version__
from diligens.cli import app
from diligens.config import Settings
from diligens.models import Flag

FIXTURES = Path(__file__).parent / "fixtures"


def test_cli_help() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "analyze" in result.output


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_settings_load_without_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.diligens_model.startswith("claude-")
    assert settings.llm_enabled is False


def test_blank_api_key_counts_as_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.anthropic_api_key is None
    assert settings.llm_enabled is False


def test_flag_review_threshold() -> None:
    flag = Flag(
        voucher_ref="B2",
        account="6550",
        amount=Decimal("40000.00"),
        category="agarrelaterad",
        rationale="Konsultarvode till ägarens bolag.",
        confidence=0.6,
    )
    assert flag.needs_review is True


def test_minimal_fixture_is_cp437_sie4() -> None:
    raw = (FIXTURES / "minimal.se").read_bytes()
    text = raw.decode("cp437")
    assert "#SIETYP 4" in text
    assert "Testbolaget Åkeri AB" in text
    expected = json.loads((FIXTURES / "minimal_expected.json").read_text(encoding="utf-8"))
    assert expected["voucher_count"] == text.count("#VER ")


def test_mcp_server_imports() -> None:
    from diligens.mcp_server import mcp

    assert mcp.name == "diligens"


def test_cli_generate(tmp_path: Path) -> None:
    out = tmp_path / "demo.se"
    result = CliRunner().invoke(app, ["generate", "--out", str(out), "--seed", "7"])
    assert result.exit_code == 0, result.output
    assert out.exists() and (tmp_path / "facit.json").exists()


def test_quoted_keys_from_docker_env_files_are_unquoted(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # `docker run --env-file .env` passes KEY="value" with the quotes; .env readers do not.
    monkeypatch.setenv("SCB_API_KEY", '"key-not-real"')
    monkeypatch.setenv("BOLAGSVERKET_CLIENT_ID", "'id-not-real'")
    monkeypatch.setenv("ANTHROPIC_API_KEY", '""')
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.scb_api_key is not None
    assert settings.scb_api_key.get_secret_value() == "key-not-real"
    assert settings.bolagsverket_client_id is not None
    assert settings.bolagsverket_client_id.get_secret_value() == "id-not-real"
    assert settings.anthropic_api_key is None  # quoted empty is still missing

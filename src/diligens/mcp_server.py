"""MCP server exposing the Diligens pipeline as tools (M5).

Run with `uv run diligens-mcp`. Tools call the same functions as the CLI.
MCP Python SDK 2.x: the server class is MCPServer (FastMCP was renamed in v2).
"""

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("diligens")


@mcp.tool()
def analysera_sie(sie_path: str) -> str:
    """Analysera en SIE4 fil: resultat- och balansräkning per månad, LTM, EBITDA,
    rörelsekapital, nettoskuld och avstämning. Returnerar en sammanfattning i TSEK."""
    return "Inte implementerat ännu (M2)."


@mcp.tool()
def flagga_poster(sie_path: str) -> str:
    """Hitta normaliseringskandidater (engångsposter, ägarrelaterade kostnader,
    närstående, periodiseringar) med verifikationsreferens och säkerhet."""
    return "Inte implementerat ännu (M3)."


@mcp.tool()
def exportera_excel(sie_path: str, out_dir: str = "output") -> str:
    """Skapa DD underlag i Excel och ett kort memo. Returnerar sökvägen till filerna."""
    return "Inte implementerat ännu (M4)."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

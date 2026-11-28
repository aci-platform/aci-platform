"""ACI as an MCP server (stdio). Works with any MCP client, e.g. Claude Desktop.

Requires: pip install mcp httpx
Environment: ACI_API_KEY, ACI_BASE_URL

Example MCP client config:
{
  "mcpServers": {
    "aci": {
      "command": "python",
      "args": ["/full/path/to/aci_mcp_server.py"],
      "env": {"ACI_API_KEY": "aci_test_...", "ACI_BASE_URL": "https://YOUR-APP.onrender.com"}
    }
  }
}
"""
from mcp.server.fastmcp import FastMCP

from aci_core import authorize_purchase as _authorize
from aci_core import claim_approved_card as _claim

mcp = FastMCP("ACI")


@mcp.tool()
def authorize_purchase(amount: float, merchant: str, category: str) -> str:
    """Ask for spending authorization BEFORE buying anything.
    Returns ALLOW (with a single-use card), DECLINE, or REVIEW (a human must approve).
    Never make a purchase unless the answer starts with ALLOW."""
    return _authorize(amount, merchant, category)


@mcp.tool()
def claim_approved_card(decision_id: str) -> str:
    """After a human has approved a REVIEW decision, call this with the decision id
    to receive the single-use card."""
    return _claim(decision_id)


if __name__ == "__main__":
    mcp.run()

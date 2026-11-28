ACI: Agentic Control Interface

A provider-independent financial authority layer for AI agents. Set a spending mandate (max transaction, monthly budget, allowed merchants and categories). Your agent asks ACI before it spends and gets back ALLOW, DECLINE or REVIEW (human approval). On ALLOW, ACI issues a single-use virtual card capped at the exact amount.

Status: sandbox. Cards are mock cards from a sandbox issuer. No real funds move. Stripe Issuing is the first real issuer integration planned.

Quick start
python
from aci_sdk import ACI

aci = ACI(api_key="aci_test_...", base_url="https://YOUR-APP.onrender.com")
d = aci.authorize(amount=85.00, merchant="amazon.co.uk", category="toner")

if d.allowed:
    print(d.card)            # Card(****4272, limit=85.00 GBP, sandbox=True)
elif d.needs_review:
    print("Waiting for human approval:", d.evidence_id)
else:
    print("Declined:", d.reason)

You can also set ACI_API_KEY and ACI_BASE_URL as environment variables.

Add ACI to your LangChain agent in 3 lines of code
bash
pip install langchain-core httpx
export ACI_API_KEY="aci_test_..."          # from the sandbox signup
export ACI_BASE_URL="https://YOUR-APP.onrender.com"
python
from aci_langchain import aci_tools            # 1. import the tools
agent = create_agent(model, tools=aci_tools)   # 2. give them to your agent
# 3. done: the agent now asks ACI before it spends

That adds two tools:

Tool	What it does
authorize_purchase(amount, merchant, category)	Returns ALLOW with a single-use card capped at the amount, DECLINE with a reason, or REVIEW if a human must approve
claim_approved_card(decision_id)	After a human approves a REVIEW, returns the single-use card

On DECLINE, REVIEW or any error, the tool text tells the agent not to purchase. If ACI is unreachable, the tools fail closed.

Use it with any MCP client (for example Claude Desktop)
bash
pip install mcp httpx
json
{
  "mcpServers": {
    "aci": {
      "command": "python",
      "args": ["/full/path/to/aci_mcp_server.py"],
      "env": {"ACI_API_KEY": "aci_test_...", "ACI_BASE_URL": "https://YOUR-APP.onrender.com"}
    }
  }
}
Card details and the model

By default the tools return only the card's last four digits and limit, so the full number never enters the model's context. For a sandbox demo where the agent should receive the full card, set ACI_REVEAL_CARD=1. In production, pass the card straight to your checkout step and keep it out of the model.

How it works
ALLOW: within the mandate. A single-use card is issued for the exact amount.
DECLINE: outside the mandate (frozen agent, over the limit, merchant or category not allowed, or monthly budget exceeded). No card is issued.
REVIEW: above the agent's approval threshold. A human approves in the dashboard, then the agent claims the card with aci.claim(evidence_id).
Budget reservation is atomic, so concurrent requests cannot overspend a budget (covered by the test suite).
Idempotency-Key is supported on /v1/authorize so retries do not double-spend.
Run it locally
bash
pip install fastapi uvicorn sqlalchemy
export ADMIN_TOKEN="choose-a-long-random-value"
uvicorn main:app --reload
Tests
bash
pip install pytest httpx langchain-core mcp
pytest -v
Status

Sandbox only. Cards come from a mock issuer and no real money moves. Stripe Issuing is planned, not built. Tested under concurrent requests in our test suite (5 simultaneous requests against a limited budget never overspend).
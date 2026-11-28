"""agent_demo.py: simulates an AI procurement agent using ACI (sandbox).

Setup:
    export ACI_API_KEY="aci_test_..."          # the demo agent's key
    export ACI_BASE_URL="https://YOUR-APP.onrender.com"
Run:
    python agent_demo.py            # card shown masked
    python agent_demo.py --show-card
"""
import os
import sys
import time
import uuid

from aci_sdk import ACI, ACIError

SHOW_CARD = "--show-card" in sys.argv

REASONS = {
    "EXCEEDS_TRANSACTION_LIMIT": "this is above the agent's per-purchase limit",
    "MERCHANT_NOT_ALLOWED": "this merchant is not on the approved list",
    "CATEGORY_NOT_ALLOWED": "this category is not approved for this agent",
    "EXCEEDS_MONTHLY_BUDGET": "this would exceed the agent's monthly budget",
    "AGENT_FROZEN": "this agent has been frozen by a human",
}


def pause(seconds: float = 1.2) -> None:
    time.sleep(seconds)  # slows the output down so it reads well on a screen share or recording


class ProcurementAgent:
    """A stand-in for an LLM-driven agent. The key point: it asks ACI BEFORE it spends."""

    def __init__(self, aci: ACI):
        self.aci = aci

    def try_to_buy(self, item: str, amount: float, merchant: str, category: str):
        print(f"\n🤖 Agent: I need to buy '{item}' for £{amount:.2f} from {merchant}.")
        pause()
        print("🤖 Agent: Checking my spending authority with ACI first...")
        pause()

        decision = self.aci.authorize(
            amount=amount,
            merchant=merchant,
            category=category,
            idempotency_key=f"demo-{uuid.uuid4().hex[:12]}",  # safe to retry without double-spending
        )

        if decision.allowed:
            print(f"✅ ACI: ALLOW ({decision.reason}). Evidence ID: {decision.evidence_id}")
            print(f"💳 Single-use card issued: {decision.card}")
            if SHOW_CARD:
                c = decision.card
                print(f"   Number {c.number}  CVC {c.cvc}  Exp {c.exp_month:02d}/{c.exp_year}")
            print("🤖 Agent: Got a card capped at exactly that amount. Completing checkout.")
        elif decision.needs_review:
            print(f"⏸️  ACI: REVIEW ({decision.reason}). Evidence ID: {decision.evidence_id}")
            print("🤖 Agent: A human must approve this first. I'll wait and claim the card later.")
        else:
            why = REASONS.get(decision.reason, decision.reason)
            print(f"⛔ ACI: DECLINE ({decision.reason}). Evidence ID: {decision.evidence_id}")
            print(f"🤖 Agent: Blocked: {why}. No card was issued, so I cannot buy this.")
        return decision


def main() -> int:
    api_key = os.getenv("ACI_API_KEY")
    base_url = os.getenv("ACI_BASE_URL")
    if not api_key or not base_url:
        print("Set ACI_API_KEY and ACI_BASE_URL first (see the top of this file).")
        return 1

    print("=" * 62)
    print(" ACI sandbox demo: an AI agent buying office supplies")
    print("=" * 62)
    print("(The first request can take up to a minute if the server was asleep.)")

    agent = ProcurementAgent(ACI(api_key=api_key, base_url=base_url))
    try:
        agent.try_to_buy("Printer toner bundle", 85.00, "amazon.co.uk", "office_supplies")
        agent.try_to_buy("Ergonomic office chairs (x4)", 200.00, "amazon.co.uk", "office_supplies")
    except ACIError as exc:
        print(f"\nCould not reach ACI: {exc}")
        if exc.status_code == 401:
            print("The API key is wrong, or the agent was deleted. Create a new agent and key.")
        return 1

    print("\n" + "=" * 62)
    print(" Done. Every decision above is recorded in the audit ledger.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
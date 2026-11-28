"""ACI as LangChain tools.

    from aci_langchain import aci_tools
    agent = create_agent(model, tools=aci_tools)   # or pass to any LangChain agent

Requires: pip install langchain-core httpx
Environment: ACI_API_KEY, ACI_BASE_URL
"""
from langchain_core.tools import tool

from aci_core import authorize_purchase as _authorize
from aci_core import claim_approved_card as _claim


@tool
def authorize_purchase(amount: float, merchant: str, category: str) -> str:
    """Ask for spending authorization BEFORE buying anything.
    Returns ALLOW (with a single-use card), DECLINE, or REVIEW (a human must approve).
    Never make a purchase unless the answer starts with ALLOW."""
    return _authorize(amount, merchant, category)


@tool
def claim_approved_card(decision_id: str) -> str:
    """After a human has approved a REVIEW decision, call this with the decision id
    to receive the single-use card."""
    return _claim(decision_id)


aci_tools = [authorize_purchase, claim_approved_card]

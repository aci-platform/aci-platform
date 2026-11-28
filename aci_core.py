"""Shared logic for ACI's agent-facing tools (used by the LangChain tool and the MCP server).

Design rules:
  * Fail closed: any error or unexpected answer tells the agent NOT to purchase.
  * By default the model never sees the card number. Set ACI_REVEAL_CARD=1 for a
    sandbox demo where the agent should receive full card details.
  * Identical requests made within 60 seconds share an Idempotency-Key, so a framework
    retry cannot reserve budget twice.
"""
import hashlib
import os
import time
from typing import Optional

from aci_sdk import ACI, ACIError, Decision

IDEMPOTENCY_WINDOW_SECONDS = 60

_client: Optional[ACI] = None


def get_client() -> ACI:
    """Create the ACI client once, reading ACI_API_KEY and ACI_BASE_URL from the environment."""
    global _client
    if _client is None:
        _client = ACI()
    return _client


def _reveal_default() -> bool:
    return os.getenv("ACI_REVEAL_CARD", "").strip().lower() in ("1", "true", "yes")


def make_idempotency_key(amount: float, merchant: str, category: str,
                         now: Optional[float] = None) -> str:
    """Same purchase request within the same 60-second window gives the same key."""
    bucket = int((time.time() if now is None else now) // IDEMPOTENCY_WINDOW_SECONDS)
    raw = f"{amount:.2f}|{merchant.strip().lower()}|{category.strip().lower()}|{bucket}"
    return "tool-" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def format_decision(d: Decision, reveal_card: bool = False) -> str:
    """Turn a Decision into a short instruction the agent can act on."""
    if d.decision == "ALLOW" and d.card:
        c = d.card
        base = (f"ALLOW. Single-use card ending {c.last4}, limit "
                f"{c.spending_limit_cents / 100:.2f} {c.currency}"
                f"{' (sandbox)' if c.sandbox else ''}. Decision id: {d.evidence_id}.")
        if reveal_card:
            base += f" Card number {c.number}, cvc {c.cvc}, expiry {c.exp_month}/{c.exp_year}."
        return base
    if d.decision == "ALLOW":
        # Replayed request: ACI issued the card on the first request and does not store it.
        return (f"ALLOW was already granted for this identical request (decision id: "
                f"{d.evidence_id}). The card was returned the first time and cannot be shown "
                "again. Use that card. Do NOT request the same purchase again.")
    if d.decision == "REVIEW":
        return (f"REVIEW: a human must approve this purchase. Decision id: {d.evidence_id}. "
                "Do NOT purchase yet. Once approved, call claim_approved_card with this id.")
    if d.decision == "DECLINE":
        return f"DECLINE: {d.reason}. Do NOT make this purchase. Decision id: {d.evidence_id}."
    return f"ERROR: unexpected answer '{d.decision}'. Do NOT make this purchase."


def authorize_purchase(amount: float, merchant: str, category: str,
                       client: Optional[ACI] = None,
                       reveal_card: Optional[bool] = None) -> str:
    """Ask ACI for permission to spend. Returns text starting ALLOW, REVIEW, DECLINE or ERROR."""
    if amount is None or amount <= 0:
        return "ERROR: amount must be greater than zero. Do NOT make this purchase."
    reveal = _reveal_default() if reveal_card is None else reveal_card
    key = make_idempotency_key(amount, merchant, category)
    try:
        d = (client or get_client()).authorize(
            amount=amount, merchant=merchant, category=category, idempotency_key=key)
    except ACIError as exc:
        return f"ERROR: could not get authorization ({exc}). Do NOT make this purchase."
    return format_decision(d, reveal_card=reveal)


def claim_approved_card(decision_id: str, client: Optional[ACI] = None,
                        reveal_card: Optional[bool] = None) -> str:
    """After a human approves a REVIEW decision, fetch the single-use card."""
    reveal = _reveal_default() if reveal_card is None else reveal_card
    try:
        d = (client or get_client()).claim(decision_id)
    except ACIError as exc:
        return (f"ERROR: could not claim a card ({exc}). It may not be approved yet. "
                "Do NOT make this purchase.")
    return format_decision(d, reveal_card=reveal)

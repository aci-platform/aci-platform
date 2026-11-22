"""ACI Python SDK: a thin client for the ACI authorization API (sandbox)."""
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx

__all__ = ["ACI", "ACIError", "Card", "Decision"]


class ACIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Card:
    number: str = field(repr=False)  # hidden from repr so it doesn't end up in logs
    cvc: str = field(repr=False)
    exp_month: int
    exp_year: int
    spending_limit_cents: int
    currency: str
    single_use: bool = True
    sandbox: bool = True

    @property
    def last4(self) -> str:
        return self.number[-4:]

    def __repr__(self) -> str:
        return (f"Card(****{self.last4}, limit={self.spending_limit_cents / 100:.2f} "
                f"{self.currency}, sandbox={self.sandbox})")


@dataclass
class Decision:
    decision: str                      # ALLOW | DECLINE | REVIEW
    reason: str
    evidence_id: str
    card: Optional[Card] = None
    review_status: Optional[str] = None
    replayed: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision == "ALLOW"

    @property
    def needs_review(self) -> bool:
        return self.decision == "REVIEW"


def _parse(data: dict) -> Decision:
    cred = data.get("payment_credentials")
    card = None
    if cred:
        card = Card(
            number=cred["number"], cvc=cred["cvc"],
            exp_month=cred["exp_month"], exp_year=cred["exp_year"],
            spending_limit_cents=cred["spending_limit_cents"], currency=cred["currency"],
            single_use=cred.get("single_use", True), sandbox=cred.get("sandbox", True),
        )
    return Decision(
        decision=data["decision"], reason=data.get("reason", ""),
        evidence_id=data["evidence_id"], card=card,
        review_status=data.get("review_status"), replayed=bool(data.get("replayed")),
    )


class ACI:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = 60.0):
        self.api_key = api_key or os.getenv("ACI_API_KEY")
        self.base_url = (base_url or os.getenv("ACI_BASE_URL") or "").rstrip("/")
        if not self.api_key:
            raise ACIError("Provide api_key or set ACI_API_KEY")
        if not self.base_url:
            raise ACIError("Provide base_url or set ACI_BASE_URL")
        # Generous timeout: free-tier hosts can take a while to wake up.
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            r = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ACIError(f"Network error: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ACIError(f"{r.status_code}: {detail}", status_code=r.status_code)
        return r.json()

    def authorize(self, amount: float, merchant: str, category: str,
                  idempotency_key: Optional[str] = None) -> Decision:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        data = self._request("POST", "/v1/authorize", headers=headers,
                             json={"amount": amount, "merchant": merchant, "category": category})
        return _parse(data)

    def get_decision(self, evidence_id: str) -> Decision:
        return _parse(self._request("GET", f"/v1/decisions/{evidence_id}"))

    def claim(self, evidence_id: str) -> Decision:
        """After a human approves a REVIEW, claim the single-use card."""
        return _parse(self._request("POST", f"/v1/reviews/{evidence_id}/claim"))

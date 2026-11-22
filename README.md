# ACI: Agentic Control Interface

A provider-independent financial authority layer for AI agents. Set a spending
mandate (max transaction, monthly budget, allowed merchants and categories). Your
agent asks ACI before it spends and gets back **ALLOW**, **DECLINE** or **REVIEW**
(human approval). On ALLOW, ACI issues a single-use virtual card capped at the
exact amount.

> **Status: sandbox.** Cards are mock cards from a sandbox issuer. No real funds
> move. Stripe Issuing is the first real issuer integration planned.

## Quick start

```python
from aci_sdk import ACI

aci = ACI(api_key="aci_test_...", base_url="https://YOUR-APP.onrender.com")
d = aci.authorize(amount=85.00, merchant="amazon.co.uk", category="toner")

if d.allowed:
    print(d.card)            # Card(****4272, limit=85.00 GBP, sandbox=True)
elif d.needs_review:
    print("Waiting for human approval:", d.evidence_id)
else:
    print("Declined:", d.reason)
```

You can also set `ACI_API_KEY` and `ACI_BASE_URL` as environment variables.

## How it works

- **ALLOW:** within the mandate. A single-use card is issued for the exact amount.
- **DECLINE:** outside the mandate (frozen agent, over the limit, merchant or
  category not allowed, or monthly budget exceeded). No card is issued.
- **REVIEW:** above the agent's approval threshold. A human approves in the
  dashboard, then the agent claims the card with `aci.claim(evidence_id)`.
- Budget reservation is atomic, so concurrent requests cannot overspend a budget
  (covered by the test suite).
- `Idempotency-Key` is supported on `/v1/authorize` so retries do not double-spend.

## Run it locally

```bash
pip install fastapi uvicorn sqlalchemy
export ADMIN_TOKEN="choose-a-long-random-value"
uvicorn main:app --reload
```

## Tests

```bash
pip install pytest httpx
pytest -v
```

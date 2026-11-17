# ACI - Agentic Control Interface

ACI is a provider-independent financial authority infrastructure for AI agents. It allows developers to issue API credentials, set spending mandates (merchant allowlists, transaction limits, monthly budgets), and evaluate purchase requests deterministically.

## Features
- **Agent Credentials:** Secure API key generation (SHA-256 hashed).
- **Policy Engine:** Deterministic `ALLOW`, `DECLINE`, and `REVIEW` decisions.
- **Budget Tracking:** Live monthly budget deduction.
- **Audit Ledger:** Full transaction history.

## Run Locally
1. `python3 -m venv venv`
2. `source venv/bin/activate`
3. `pip install -r requirements.txt`
4. `python -m uvicorn main:app --reload`
5. Open `http://127.0.0.1:8000`
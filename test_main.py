import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ---- Test environment: temp DB, test admin token, never touches ./aci.db ----
ROOT = os.path.dirname(os.path.abspath(__file__))
_tmp = tempfile.mkdtemp()
os.makedirs(os.path.join(_tmp, "static"))  # main.py mounts ./static at import time
os.environ["ADMIN_TOKEN"] = "test-admin-token"

sys.path.insert(0, ROOT)

_cwd = os.getcwd()
os.chdir(_tmp)
import main  # noqa: E402
os.chdir(_cwd)

test_engine = create_engine(
    f"sqlite:///{os.path.join(_tmp, 'test.db')}",
    connect_args={"check_same_thread": False, "timeout": 30},
)
main.Base.metadata.create_all(bind=test_engine)
with test_engine.begin() as conn:
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_agent_idem ON ledger (agent_id, idempotency_key)"
    ))
main.engine = test_engine
main.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

from fastapi.testclient import TestClient  # noqa: E402

ADMIN = {"X-Admin-Token": "test-admin-token"}


def luhn_valid(number: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(number)):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@pytest.fixture(autouse=True)
def reset_rate_limits():
    main._rl_hits.clear()
    yield
    main._rl_hits.clear()


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        yield c


_counter = {"n": 0}


def unique_email():
    _counter["n"] += 1
    return f"dev{_counter['n']}@example.com"


def make_agent(client, *, max_tx=500, monthly=2000, approval_over=1000,
               merchants=("amazon.co.uk",), categories=("toner",)):
    r = client.post("/api/agents", headers=ADMIN, json={
        "name": "test-agent",
        "max_transaction": max_tx,
        "monthly_limit": monthly,
        "allowed_merchants": list(merchants),
        "allowed_categories": list(categories),
        "require_approval_over": approval_over,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    return body["agent_id"], {"Authorization": f"Bearer {body['api_key']}"}


def authorize(client, headers, amount=30, merchant="amazon.co.uk", category="toner", extra=None):
    h = dict(headers, **(extra or {}))
    return client.post("/v1/authorize", headers=h,
                       json={"amount": amount, "merchant": merchant, "category": category})


def do_signup(client, email=None, consent=True, ip="1.2.3.4"):
    return client.post(
        "/api/signup",
        headers={"cf-connecting-ip": ip},
        json={"email": email or unique_email(), "consent": consent},
    )


# ---------------------------------------------------------------- basics

def test_admin_routes_require_token(client):
    assert client.get("/api/agents").status_code == 401
    assert client.get("/api/agents", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.get("/api/agents", headers=ADMIN).status_code == 200


def test_allow_returns_capped_sandbox_card(client):
    _, h = make_agent(client)
    r = authorize(client, h, amount=85)
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "ALLOW"
    card = body["payment_credentials"]
    assert card["number"].startswith("999999") and len(card["number"]) == 16
    assert luhn_valid(card["number"])
    assert card["spending_limit_cents"] == 8500
    assert card["single_use"] is True and card["sandbox"] is True


def test_over_limit_is_declined_with_no_card(client):
    _, h = make_agent(client, max_tx=100)
    body = authorize(client, h, amount=150).json()
    assert body["decision"] == "DECLINE"
    assert body["reason"] == "EXCEEDS_TRANSACTION_LIMIT"
    assert "payment_credentials" not in body


def test_disallowed_merchant_is_declined(client):
    _, h = make_agent(client)
    body = authorize(client, h, merchant="evil-shop.com").json()
    assert body["decision"] == "DECLINE" and body["reason"] == "MERCHANT_NOT_ALLOWED"
    assert "payment_credentials" not in body


def test_merchant_match_is_case_insensitive(client):
    _, h = make_agent(client, merchants=("Amazon.co.uk",))
    assert authorize(client, h, merchant="AMAZON.CO.UK").json()["decision"] == "ALLOW"


def test_invalid_api_key_rejected(client):
    r = authorize(client, {"Authorization": "Bearer aci_test_nope"})
    assert r.status_code == 401


# ------------------------------------------------------------ idempotency

def test_idempotent_replay_returns_same_decision_and_one_reservation(client):
    agent_id, h = make_agent(client, monthly=1000)
    first = authorize(client, h, amount=40, extra={"Idempotency-Key": "k-1"}).json()
    second = authorize(client, h, amount=40, extra={"Idempotency-Key": "k-1"}).json()
    assert first["decision"] == "ALLOW"
    assert second["evidence_id"] == first["evidence_id"]
    assert second.get("replayed") is True
    agents = {a["id"]: a for a in client.get("/api/agents", headers=ADMIN).json()}
    assert agents[agent_id]["spend_this_month"] == 40  # reserved once, not twice


# ------------------------------------------------------------ concurrency

def test_concurrent_requests_cannot_overspend_budget(client):
    """5 simultaneous £30 requests against a £100 budget: exactly 3 may pass."""
    agent_id, h = make_agent(client, max_tx=100, monthly=100)
    barrier = threading.Barrier(5)

    def fire(_):
        with TestClient(main.app) as c:
            barrier.wait()  # release all five at the same moment
            return authorize(c, h, amount=30)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(fire, range(5)))

    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    decisions = [r.json()["decision"] for r in results]
    assert decisions.count("ALLOW") == 3, decisions
    assert decisions.count("DECLINE") == 2, decisions

    agents = {a["id"]: a for a in client.get("/api/agents", headers=ADMIN).json()}
    assert agents[agent_id]["spend_this_month"] == 90  # never above the £100 limit


# ------------------------------------------------------------ review flow

def test_review_requires_approval_then_card_can_be_claimed_once(client):
    _, h = make_agent(client, approval_over=50)
    r = authorize(client, h, amount=60).json()
    assert r["decision"] == "REVIEW" and r["review_status"] == "PENDING"
    dec_id = r["evidence_id"]

    # cannot claim before approval
    assert client.post(f"/v1/reviews/{dec_id}/claim", headers=h).status_code == 409

    assert client.post(f"/api/reviews/{dec_id}/approve", headers=ADMIN).status_code == 200
    claim = client.post(f"/v1/reviews/{dec_id}/claim", headers=h)
    assert claim.status_code == 200
    assert claim.json()["payment_credentials"]["spending_limit_cents"] == 6000

    # a second claim must not issue a second card
    assert client.post(f"/v1/reviews/{dec_id}/claim", headers=h).status_code == 409


def test_rejected_review_cannot_be_claimed(client):
    _, h = make_agent(client, approval_over=50)
    dec_id = authorize(client, h, amount=60).json()["evidence_id"]
    assert client.post(f"/api/reviews/{dec_id}/reject", headers=ADMIN).status_code == 200
    assert client.post(f"/v1/reviews/{dec_id}/claim", headers=h).status_code == 403


# ------------------------------------------------------------ signup

def test_signup_requires_consent(client):
    r = do_signup(client, consent=False)
    assert r.status_code == 400


def test_signup_rejects_invalid_email(client):
    r = do_signup(client, email="not-an-email")
    assert r.status_code == 400


def test_signup_returns_working_sandbox_key_with_fixed_mandate(client):
    r = do_signup(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["api_key"].startswith("aci_test_")
    h = {"Authorization": f"Bearer {body['api_key']}"}

    ok = authorize(client, h, amount=10, merchant="amazon.co.uk", category="toner").json()
    assert ok["decision"] == "ALLOW"

    over = authorize(client, h, amount=150, merchant="amazon.co.uk", category="toner").json()
    assert over["decision"] == "DECLINE" and over["reason"] == "EXCEEDS_TRANSACTION_LIMIT"

    review = authorize(client, h, amount=60, merchant="amazon.co.uk", category="toner").json()
    assert review["decision"] == "REVIEW"


def test_signup_duplicate_email_rejected(client):
    email = unique_email()
    assert do_signup(client, email=email, ip="2.2.2.2").status_code == 201
    assert do_signup(client, email=email, ip="3.3.3.3").status_code == 409
    assert do_signup(client, email=email.upper(), ip="4.4.4.4").status_code == 409


def test_signup_per_ip_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "SIGNUP_PER_IP_PER_HOUR", 3)
    for _ in range(3):
        assert do_signup(client, ip="9.9.9.9").status_code == 201
    assert do_signup(client, ip="9.9.9.9").status_code == 429
    # a different address is unaffected
    assert do_signup(client, ip="8.8.8.8").status_code == 201


def test_signup_daily_cap(client, monkeypatch):
    total = client.get("/api/usage", headers=ADMIN).json()["signups_total"]
    monkeypatch.setattr(main, "SIGNUP_DAILY_CAP", total)
    r = do_signup(client, ip="7.7.7.7")
    assert r.status_code == 429


def test_signup_response_is_not_cacheable(client):
    r = do_signup(client, ip="6.6.6.6")
    assert r.headers.get("cache-control") == "no-store"


# ------------------------------------------------------------ rate limits

def test_agent_request_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main, "REQUESTS_PER_MINUTE", 3)
    _, h = make_agent(client)
    for _ in range(3):
        assert authorize(client, h, amount=1).status_code == 200
    assert authorize(client, h, amount=1).status_code == 429


def test_repeated_wrong_admin_token_gets_throttled(client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_FAILS_PER_MINUTE", 3)
    bad = {"X-Admin-Token": "wrong"}
    codes = [client.get("/api/agents", headers=bad).status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    # the right token is never blocked by the failure counter
    assert client.get("/api/agents", headers=ADMIN).status_code == 200


# ------------------------------------------------------------ usage

def test_usage_requires_admin_and_counts(client):
    assert client.get("/api/usage").status_code == 401
    before = client.get("/api/usage", headers=ADMIN).json()
    r = do_signup(client, ip="5.5.5.5")
    h = {"Authorization": f"Bearer {r.json()['api_key']}"}
    authorize(client, h, amount=5)
    after = client.get("/api/usage", headers=ADMIN).json()
    assert after["signups_total"] == before["signups_total"] + 1
    assert after["made_at_least_one_request"] == before["made_at_least_one_request"] + 1
    assert set(after) == {"signups_total", "signups_last_24h",
                          "made_at_least_one_request", "active_on_two_or_more_days"}

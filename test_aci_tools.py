"""Tests for the shared tool logic. Uses a fake client, so no network is needed."""
import pytest

import aci_core
from aci_sdk import ACIError, Card, Decision


def card():
    return Card(number="9999990000004272", cvc="123", exp_month=12, exp_year=2030,
                spending_limit_cents=8500, currency="GBP")


class Fake:
    def __init__(self, decision=None, error=None):
        self.decision, self.error, self.calls = decision, error, []

    def authorize(self, amount, merchant, category, idempotency_key=None):
        self.calls.append((amount, merchant, category, idempotency_key))
        if self.error:
            raise self.error
        return self.decision

    def claim(self, evidence_id):
        self.claimed = evidence_id
        if self.error:
            raise self.error
        return self.decision


ALLOW = Decision("ALLOW", "ok", "ev_1", card=card())


def test_allow_hides_card_number_by_default():
    out = aci_core.authorize_purchase(85, "amazon.co.uk", "toner", client=Fake(ALLOW), reveal_card=False)
    assert out.startswith("ALLOW") and "4272" in out and "9999990000004272" not in out and "cvc" not in out


def test_allow_reveals_card_when_asked():
    out = aci_core.authorize_purchase(85, "amazon.co.uk", "toner", client=Fake(ALLOW), reveal_card=True)
    assert "9999990000004272" in out and "cvc 123" in out


def test_env_flag_controls_reveal(monkeypatch):
    monkeypatch.setenv("ACI_REVEAL_CARD", "1")
    assert "9999990000004272" in aci_core.authorize_purchase(85, "m", "c", client=Fake(ALLOW))
    monkeypatch.delenv("ACI_REVEAL_CARD")
    assert "9999990000004272" not in aci_core.authorize_purchase(85, "m", "c", client=Fake(ALLOW))


def test_decline_tells_agent_not_to_buy():
    out = aci_core.authorize_purchase(200, "m", "c",
        client=Fake(Decision("DECLINE", "EXCEEDS_TRANSACTION_LIMIT", "ev_2")))
    assert out.startswith("DECLINE") and "EXCEEDS_TRANSACTION_LIMIT" in out and "Do NOT" in out


def test_review_points_to_claim_tool():
    out = aci_core.authorize_purchase(120, "m", "c", client=Fake(Decision("REVIEW", "NEEDS_APPROVAL", "ev_3")))
    assert out.startswith("REVIEW") and "ev_3" in out and "claim_approved_card" in out


def test_api_error_fails_closed():
    out = aci_core.authorize_purchase(10, "m", "c", client=Fake(error=ACIError("boom", 500)))
    assert out.startswith("ERROR") and "Do NOT" in out


@pytest.mark.parametrize("bad", [0, -5, None])
def test_bad_amount_never_calls_api(bad):
    fake = Fake(ALLOW)
    assert aci_core.authorize_purchase(bad, "m", "c", client=fake).startswith("ERROR")
    assert fake.calls == []


def test_claim_error_fails_closed():
    out = aci_core.claim_approved_card("ev_3", client=Fake(error=ACIError("not approved", 409)))
    assert out.startswith("ERROR") and "Do NOT" in out


def test_replayed_allow_without_card_is_not_reported_as_decline():
    replay = Decision("ALLOW", "ok", "ev_9", card=None, replayed=True)
    out = aci_core.authorize_purchase(85, "m", "c", client=Fake(replay))
    assert out.startswith("ALLOW") and "Do NOT request the same purchase again" in out


def test_unknown_decision_fails_closed():
    out = aci_core.authorize_purchase(85, "m", "c", client=Fake(Decision("MAYBE", "", "ev_8")))
    assert out.startswith("ERROR") and "Do NOT" in out


def test_authorize_sends_idempotency_key():
    fake = Fake(ALLOW)
    aci_core.authorize_purchase(85, "amazon.co.uk", "toner", client=fake)
    assert fake.calls[0][3].startswith("tool-")


def test_same_request_same_window_same_key_but_different_request_differs():
    k1 = aci_core.make_idempotency_key(85, "Amazon.co.uk", "toner", now=960)
    k2 = aci_core.make_idempotency_key(85, "amazon.co.uk ", "TONER", now=1000)
    k3 = aci_core.make_idempotency_key(86, "amazon.co.uk", "toner", now=990)
    k4 = aci_core.make_idempotency_key(85, "amazon.co.uk", "toner", now=1100)
    assert k1 == k2 and k1 != k3 and k1 != k4


def test_claim_strips_stray_spaces_from_id():
    fake = Fake(ALLOW)
    aci_core.claim_approved_card("  dec_a553d82c ", client=fake)
    assert fake.claimed == "dec_a553d82c"

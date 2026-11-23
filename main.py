import os
import re
import uuid
import time
import secrets
import hashlib
import logging
import threading
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from datetime import datetime, timezone, date, timedelta
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Response, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, Column, String, Float, Boolean, JSON, DateTime,
    inspect, text, update, or_, func,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker, Session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aci")

# ---------------------------------------------------------------------------
# Configuration (fails closed)
# ---------------------------------------------------------------------------
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")
if not ADMIN_TOKEN:
    raise RuntimeError("ADMIN_TOKEN must be set. Refusing to start with open admin routes.")

ISSUER_NAME = os.getenv("ISSUER", "mock")
CARD_CURRENCY = "GBP"
IP_SALT = os.getenv("IP_SALT", "aci-default-salt")

# Rate limits and caps (read at request time, so tests can change them)
REQUESTS_PER_MINUTE = int(os.getenv("REQUESTS_PER_MINUTE", "60"))      # per agent key
ADMIN_FAILS_PER_MINUTE = int(os.getenv("ADMIN_FAILS_PER_MINUTE", "10"))  # failed admin attempts per IP
SIGNUP_PER_IP_PER_HOUR = int(os.getenv("SIGNUP_PER_IP_PER_HOUR", "3"))
SIGNUP_DAILY_CAP = int(os.getenv("SIGNUP_DAILY_CAP", "50"))

# Fixed, safe mandate for every self-serve sandbox agent
SANDBOX_MANDATE = {
    "max_transaction": 100.0,
    "monthly_limit": 500.0,
    "allowed_merchants": ["amazon.co.uk", "viking-direct.co.uk"],
    "allowed_categories": ["toner", "office_supplies", "software"],
    "require_approval_over": 50.0,
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
SQLALCHEMY_DATABASE_URL = "sqlite:///./aci.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def hash_ip(ip: str) -> str:
    return hashlib.sha256((IP_SALT + ip).encode()).hexdigest()[:32]


def utcnow():
    return datetime.now(timezone.utc)


def current_period() -> str:
    return utcnow().strftime("%Y-%m")


def norm(value: str) -> str:
    return value.strip().lower()


class Agent(Base):
    __tablename__ = "agents"
    id = Column(String, primary_key=True, index=True)
    name = Column(String)
    api_key_hash = Column(String, unique=True, index=True)
    max_transaction = Column(Float)
    monthly_limit = Column(Float)
    spend_this_month = Column(Float, default=0.0)
    period = Column(String, nullable=True)
    allowed_merchants = Column(JSON)
    allowed_categories = Column(JSON)
    require_approval_over = Column(Float)
    is_frozen = Column(Boolean, default=False)
    deleted_at = Column(DateTime, nullable=True)


class Ledger(Base):
    __tablename__ = "ledger"
    id = Column(String, primary_key=True, index=True)
    agent_id = Column(String)
    amount = Column(Float)
    merchant = Column(String)
    category = Column(String)
    decision = Column(String)
    reason = Column(String)
    issuer_card_id = Column(String, nullable=True)
    review_status = Column(String, nullable=True)
    idempotency_key = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Signup(Base):
    __tablename__ = "signups"
    id = Column(String, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    agent_id = Column(String, index=True)
    ip_hash = Column(String)          # hashed, never the raw IP
    consent_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, index=True)


Base.metadata.create_all(bind=engine)


def ensure_schema():
    """Stop-gap migration for the prototype. Replace with Alembic when you move to Postgres."""
    wanted = {
        "agents": {"period": "VARCHAR", "deleted_at": "DATETIME"},
        "ledger": {
            "issuer_card_id": "VARCHAR",
            "review_status": "VARCHAR",
            "idempotency_key": "VARCHAR",
            "created_at": "DATETIME",
        },
    }
    insp = inspect(engine)
    for table, cols in wanted.items():
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl_type in cols.items():
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_agent_idem "
            "ON ledger (agent_id, idempotency_key)"
        ))


ensure_schema()

# ---------------------------------------------------------------------------
# Rate limiting (in memory: fine for a single server process)
# ---------------------------------------------------------------------------
_rl_lock = threading.Lock()
_rl_hits = defaultdict(deque)


def rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Records a hit. Returns False when the key is over its limit for the window."""
    now = time.monotonic()
    with _rl_lock:
        q = _rl_hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def enforce_rate_limit(key: str, limit: int, window_seconds: int, message: str = "Rate limit exceeded"):
    if not rate_limit(key, limit, window_seconds):
        raise HTTPException(
            status_code=429, detail=message,
            headers={"Retry-After": str(window_seconds)},
        )


def client_ip(request: Request) -> str:
    # Render sits behind Cloudflare. These headers can be spoofed, so the per-IP limit is
    # a speed bump; the daily signup cap is the real backstop.
    ip = request.headers.get("cf-connecting-ip")
    if not ip:
        fwd = request.headers.get("x-forwarded-for")
        ip = fwd.split(",")[0].strip() if fwd else None
    if not ip and request.client:
        ip = request.client.host
    return ip or "unknown"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    max_transaction: float = Field(gt=0)
    monthly_limit: float = Field(gt=0)
    allowed_merchants: List[str]
    allowed_categories: List[str]
    require_approval_over: float = Field(ge=0)


class AuthRequest(BaseModel):
    amount: float = Field(ge=0.01, le=1_000_000)
    merchant: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)


class SignupRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    consent: bool


# ---------------------------------------------------------------------------
# App + dependencies
# ---------------------------------------------------------------------------
app = FastAPI()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_admin(request: Request, x_admin_token: str = Header(None)):
    if not secrets.compare_digest(x_admin_token or "", ADMIN_TOKEN):
        # Only failures count, so brute-forcing the token gets throttled.
        enforce_rate_limit(f"adminfail:{client_ip(request)}", ADMIN_FAILS_PER_MINUTE, 60,
                           "Too many failed attempts")
        raise HTTPException(status_code=401, detail="Admin token required")


def agent_public(a: Agent) -> dict:
    hidden = {"api_key_hash"}
    return {c.name: getattr(a, c.name) for c in Agent.__table__.columns if c.name not in hidden}


def roll_period(db: Session, agent: Agent) -> None:
    """Reset monthly spend atomically when the calendar month changes."""
    period = current_period()
    if agent.period != period:
        db.execute(
            update(Agent)
            .where(Agent.id == agent.id, or_(Agent.period.is_(None), Agent.period != period))
            .values(spend_this_month=0.0, period=period)
        )
        db.commit()
        db.refresh(agent)


def get_agent(authorization: str = Header(None), db: Session = Depends(get_db)) -> Agent:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ", 1)[1]
    agent = (
        db.query(Agent)
        .filter(Agent.api_key_hash == hash_key(token), Agent.deleted_at.is_(None))
        .first()
    )
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    enforce_rate_limit(f"agent:{agent.id}", REQUESTS_PER_MINUTE, 60, "Rate limit exceeded for this key")
    roll_period(db, agent)
    return agent


def get_active_agent(db: Session, agent_id: str) -> Agent:
    agent = db.query(Agent).filter(Agent.id == agent_id, Agent.deleted_at.is_(None)).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def new_agent_record(*, name, max_transaction, monthly_limit, merchants, categories, approval_over):
    """Builds an Agent plus its raw API key (the raw key is only ever returned once)."""
    agent_id = f"agent_{uuid.uuid4().hex[:8]}"
    raw_api_key = f"aci_test_{secrets.token_urlsafe(24)}"
    agent = Agent(
        id=agent_id, name=name.strip(), api_key_hash=hash_key(raw_api_key),
        max_transaction=max_transaction, monthly_limit=monthly_limit,
        spend_this_month=0.0, period=current_period(),
        allowed_merchants=[norm(m) for m in merchants],
        allowed_categories=[norm(c) for c in categories],
        require_approval_over=approval_over,
    )
    return agent, raw_api_key


# ---------------------------------------------------------------------------
# Issuer adapter
# ---------------------------------------------------------------------------


class Issuer(ABC):
    @abstractmethod
    def create_single_use_card(self, amount_cents: int, currency: str, merchant: str) -> dict: ...


def luhn_check_digit(partial: str) -> str:
    total = 0
    for i, d in enumerate(reversed(partial)):
        n = int(d)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return str((10 - total % 10) % 10)


class MockIssuer(Issuer):
    BIN = "999999"  # Fake BIN, cannot be mistaken for a live card

    def create_single_use_card(self, amount_cents, currency, merchant):
        body = self.BIN + "".join(str(secrets.randbelow(10)) for _ in range(9))
        number = body + luhn_check_digit(body)
        today = date.today()
        return {
            "id": f"sbx_card_{uuid.uuid4().hex[:12]}",
            "number": number,
            "cvc": f"{secrets.randbelow(1000):03d}",
            "exp_month": today.month,
            "exp_year": today.year + 1,
            "spending_limit_cents": amount_cents,
            "currency": currency,
            "single_use": True,
            "status": "active",
            "issuer": "mock",
            "sandbox": True,
        }


def build_issuer() -> Issuer:
    if ISSUER_NAME == "mock":
        return MockIssuer()
    raise RuntimeError(f"Unsupported ISSUER '{ISSUER_NAME}'. Only 'mock' is implemented.")


issuer = build_issuer()


def to_cents(amount: float) -> int:
    return int(round(amount * 100))


# ---------------------------------------------------------------------------
# Policy engine + budget reservation
# ---------------------------------------------------------------------------


def evaluate_policy(agent: Agent, request: AuthRequest):
    """Pure first-pass evaluation. The final budget check is the atomic reserve_budget()."""
    merchants = {norm(m) for m in agent.allowed_merchants}
    categories = {norm(c) for c in agent.allowed_categories}
    if agent.is_frozen:
        return "DECLINE", "AGENT_FROZEN"
    if request.amount > agent.max_transaction:
        return "DECLINE", "EXCEEDS_TRANSACTION_LIMIT"
    if norm(request.merchant) not in merchants:
        return "DECLINE", "MERCHANT_NOT_ALLOWED"
    if norm(request.category) not in categories:
        return "DECLINE", "CATEGORY_NOT_ALLOWED"
    if (agent.spend_this_month + request.amount) > agent.monthly_limit:
        return "DECLINE", "EXCEEDS_MONTHLY_BUDGET"
    if request.amount > agent.require_approval_over:
        return "REVIEW", "REQUIRES_HUMAN_APPROVAL"
    return "ALLOW", "WITHIN_POLICY"


def reserve_budget(db: Session, agent_id: str, amount: float) -> bool:
    """Atomic check-and-increment. Two concurrent requests cannot both pass."""
    res = db.execute(
        update(Agent)
        .where(
            Agent.id == agent_id,
            Agent.is_frozen.is_(False),
            Agent.deleted_at.is_(None),
            Agent.spend_this_month + amount <= Agent.monthly_limit,
        )
        .values(spend_this_month=Agent.spend_this_month + amount)
    )
    return res.rowcount == 1


# ---------------------------------------------------------------------------
# Ledger + idempotency helpers
# ---------------------------------------------------------------------------


def find_by_key(db: Session, agent_id: str, key: str) -> Optional[Ledger]:
    return db.query(Ledger).filter(Ledger.agent_id == agent_id, Ledger.idempotency_key == key).first()


def record_decision(db: Session, decision_id: str, agent_id: str, request: AuthRequest,
                    decision: str, reason: str, *, card_id: str = None,
                    review_status: str = None, key: str = None):
    """Adds a ledger row and commits (together with any pending budget reservation).
    Returns (row, created). If a concurrent request already used the idempotency key,
    rolls back and returns that existing row with created=False."""
    row = Ledger(
        id=decision_id, agent_id=agent_id, amount=request.amount,
        merchant=request.merchant, category=request.category,
        decision=decision, reason=reason, issuer_card_id=card_id,
        review_status=review_status, idempotency_key=key,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = find_by_key(db, agent_id, key) if key else None
        if existing:
            return existing, False
        raise
    return row, True


def replay_response(row: Ledger) -> dict:
    body = {
        "decision": row.decision,
        "reason": row.reason,
        "evidence_id": row.id,
        "replayed": True,
    }
    if row.decision == "REVIEW":
        body["review_status"] = row.review_status
    if row.decision == "ALLOW":
        body["note"] = "Card credentials were returned on the original request and are not stored."
    return body


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Public: self-serve sandbox signup
# ---------------------------------------------------------------------------


@app.post("/api/signup", status_code=201)
def signup(body: SignupRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """Gives a developer their own sandbox agent and API key. No admin access involved."""
    response.headers["Cache-Control"] = "no-store"  # the response contains an API key

    if not body.consent:
        raise HTTPException(status_code=400, detail="Consent is required to create a sandbox key")
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address")

    ip_h = hash_ip(client_ip(request))
    enforce_rate_limit(f"signup:{ip_h}", SIGNUP_PER_IP_PER_HOUR, 3600,
                       "Too many signups from this address. Try again later.")

    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = db.query(func.count(Signup.id)).filter(Signup.created_at >= day_start).scalar() or 0
    if today_count >= SIGNUP_DAILY_CAP:
        raise HTTPException(status_code=429, detail="Sandbox signups are full for today. Please try again tomorrow.")

    if db.query(Signup).filter(Signup.email == email).first():
        raise HTTPException(
            status_code=409,
            detail="This email already has a sandbox key. Keys are shown only once, so contact us for a new one.",
        )

    agent, raw_key = new_agent_record(
        name=f"Sandbox ({email.split('@')[0][:40]})",
        max_transaction=SANDBOX_MANDATE["max_transaction"],
        monthly_limit=SANDBOX_MANDATE["monthly_limit"],
        merchants=SANDBOX_MANDATE["allowed_merchants"],
        categories=SANDBOX_MANDATE["allowed_categories"],
        approval_over=SANDBOX_MANDATE["require_approval_over"],
    )
    now = utcnow()
    db.add(agent)
    db.add(Signup(id=f"su_{uuid.uuid4().hex[:10]}", email=email, agent_id=agent.id,
                  ip_hash=ip_h, consent_at=now, created_at=now))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="This email already has a sandbox key.")

    return {"agent_id": agent.id, "api_key": raw_key, "mandate": SANDBOX_MANDATE}


# ---------------------------------------------------------------------------
# Admin / dashboard routes
# ---------------------------------------------------------------------------


@app.post("/api/agents", status_code=201, dependencies=[Depends(require_admin)])
def create_agent(agent: AgentCreate, db: Session = Depends(get_db)):
    db_agent, raw_api_key = new_agent_record(
        name=agent.name, max_transaction=agent.max_transaction, monthly_limit=agent.monthly_limit,
        merchants=agent.allowed_merchants, categories=agent.allowed_categories,
        approval_over=agent.require_approval_over,
    )
    db.add(db_agent)
    db.commit()
    return {"agent_id": db_agent.id, "api_key": raw_api_key}


@app.get("/api/agents", dependencies=[Depends(require_admin)])
def list_agents(db: Session = Depends(get_db)):
    return [agent_public(a) for a in db.query(Agent).filter(Agent.deleted_at.is_(None)).all()]


@app.post("/api/agents/{agent_id}/freeze", dependencies=[Depends(require_admin)])
def toggle_freeze(agent_id: str, db: Session = Depends(get_db)):
    agent = get_active_agent(db, agent_id)
    agent.is_frozen = not agent.is_frozen
    db.commit()
    return {"status": "ok", "is_frozen": agent.is_frozen}


@app.delete("/api/agents/{agent_id}", dependencies=[Depends(require_admin)])
def delete_agent(agent_id: str, db: Session = Depends(get_db)):
    """Soft delete: the agent stops working, but its audit history stays in the ledger."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent and agent.deleted_at is None:
        agent.deleted_at = utcnow()
        agent.is_frozen = True
        db.commit()
    return {"status": "deleted"}


@app.get("/api/ledger", dependencies=[Depends(require_admin)])
def get_ledger(db: Session = Depends(get_db)):
    return db.query(Ledger).order_by(Ledger.created_at.desc()).limit(10).all()


@app.get("/api/usage", dependencies=[Depends(require_admin)])
def usage(db: Session = Depends(get_db)):
    """Traction numbers for self-serve signups. Emails are not returned."""
    total = db.query(func.count(Signup.id)).scalar() or 0
    last_24h = (
        db.query(func.count(Signup.id))
        .filter(Signup.created_at >= utcnow() - timedelta(hours=24))
        .scalar() or 0
    )
    rows = (
        db.query(
            Signup.agent_id,
            func.count(Ledger.id),
            func.count(func.distinct(func.date(Ledger.created_at))),
        )
        .outerjoin(Ledger, Ledger.agent_id == Signup.agent_id)
        .group_by(Signup.agent_id)
        .all()
    )
    return {
        "signups_total": total,
        "signups_last_24h": last_24h,
        "made_at_least_one_request": sum(1 for r in rows if r[1] > 0),
        "active_on_two_or_more_days": sum(1 for r in rows if r[2] >= 2),
    }


# --- Human review workflow (admin side) ---


@app.get("/api/reviews", dependencies=[Depends(require_admin)])
def list_reviews(db: Session = Depends(get_db)):
    return (
        db.query(Ledger)
        .filter(Ledger.decision == "REVIEW", Ledger.review_status == "PENDING")
        .order_by(Ledger.created_at.desc())
        .all()
    )


def _set_review_status(db: Session, decision_id: str, new_status: str):
    res = db.execute(
        update(Ledger)
        .where(Ledger.id == decision_id, Ledger.decision == "REVIEW", Ledger.review_status == "PENDING")
        .values(review_status=new_status)
    )
    if res.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Review not found or already decided")
    db.commit()
    return {"evidence_id": decision_id, "review_status": new_status}


@app.post("/api/reviews/{decision_id}/approve", dependencies=[Depends(require_admin)])
def approve_review(decision_id: str, db: Session = Depends(get_db)):
    return _set_review_status(db, decision_id, "APPROVED")


@app.post("/api/reviews/{decision_id}/reject", dependencies=[Depends(require_admin)])
def reject_review(decision_id: str, db: Session = Depends(get_db)):
    return _set_review_status(db, decision_id, "REJECTED")


# Dashboard "test" button: DRY RUN. No budget reserved, no ledger entry, no card.
@app.post("/api/agents/{agent_id}/test", dependencies=[Depends(require_admin)])
def test_purchase(agent_id: str, request: AuthRequest, db: Session = Depends(get_db)):
    agent = get_active_agent(db, agent_id)
    roll_period(db, agent)
    decision, reason = evaluate_policy(agent, request)
    return {"decision": decision, "reason": reason, "dry_run": True}


# ---------------------------------------------------------------------------
# Agent-facing endpoints
# ---------------------------------------------------------------------------


@app.post("/v1/authorize")
def authorize_purchase(
    request: AuthRequest,
    response: Response,
    idempotency_key: Optional[str] = Header(None),
    agent: Agent = Depends(get_agent),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"  # responses may contain card credentials
    agent_id = agent.id

    if idempotency_key:
        if len(idempotency_key) > 128:
            raise HTTPException(status_code=400, detail="Idempotency-Key too long (max 128)")
        existing = find_by_key(db, agent_id, idempotency_key)
        if existing:
            return replay_response(existing)

    decision_id = f"dec_{uuid.uuid4().hex[:8]}"
    decision, reason = evaluate_policy(agent, request)

    # DECLINE / REVIEW: log and return. No money reserved, no card issued.
    if decision in ("DECLINE", "REVIEW"):
        row, created = record_decision(
            db, decision_id, agent_id, request, decision, reason,
            review_status="PENDING" if decision == "REVIEW" else None,
            key=idempotency_key,
        )
        if not created:
            return replay_response(row)
        body = {"decision": decision, "reason": reason, "evidence_id": decision_id}
        if decision == "REVIEW":
            body["review_status"] = "PENDING"
        return body

    # ALLOW: atomically reserve budget, issue the card, then commit both together.
    if not reserve_budget(db, agent_id, request.amount):
        db.rollback()
        db.refresh(agent)
        d, r = evaluate_policy(agent, request)
        if d != "DECLINE":
            r = "EXCEEDS_MONTHLY_BUDGET"
        row, created = record_decision(
            db, decision_id, agent_id, request, "DECLINE", r, key=idempotency_key
        )
        if not created:
            return replay_response(row)
        return {"decision": "DECLINE", "reason": r, "evidence_id": decision_id}

    try:
        credentials = issuer.create_single_use_card(
            amount_cents=to_cents(request.amount),
            currency=CARD_CURRENCY,
            merchant=request.merchant,
        )
    except Exception as exc:
        db.rollback()  # releases the reservation
        logger.error("Card issuance failed for %s (%s): %s", agent_id, decision_id, exc)
        # No idempotency key stored on errors, so the agent can safely retry.
        record_decision(db, decision_id, agent_id, request, "ERROR", "ISSUER_ERROR")
        raise HTTPException(
            status_code=500,
            detail={
                "decision": "ERROR",
                "reason": "ISSUER_ERROR",
                "message": "Card could not be issued. No funds were reserved. Retry later.",
                "evidence_id": decision_id,
            },
        )

    card_id = credentials.pop("id")
    row, created = record_decision(
        db, decision_id, agent_id, request, "ALLOW", reason,
        card_id=card_id, key=idempotency_key,
    )
    if not created:
        return replay_response(row)

    return {
        "decision": "ALLOW",
        "reason": reason,
        "evidence_id": decision_id,
        "payment_credentials": credentials,
    }


@app.get("/v1/decisions/{decision_id}")
def get_decision(decision_id: str, response: Response,
                 agent: Agent = Depends(get_agent), db: Session = Depends(get_db)):
    """Lets an agent poll the status of a REVIEW decision."""
    response.headers["Cache-Control"] = "no-store"
    row = db.query(Ledger).filter(Ledger.id == decision_id, Ledger.agent_id == agent.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Decision not found")
    return {
        "evidence_id": row.id,
        "decision": row.decision,
        "reason": row.reason,
        "review_status": row.review_status,
    }


@app.post("/v1/reviews/{decision_id}/claim")
def claim_review(decision_id: str, response: Response,
                 agent: Agent = Depends(get_agent), db: Session = Depends(get_db)):
    """After a human approves a REVIEW, the agent claims its single-use card here."""
    response.headers["Cache-Control"] = "no-store"
    agent_id = agent.id
    row = (
        db.query(Ledger)
        .filter(Ledger.id == decision_id, Ledger.agent_id == agent_id, Ledger.decision == "REVIEW")
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Review not found")
    if row.review_status == "PENDING":
        raise HTTPException(status_code=409, detail="Awaiting human approval")
    if row.review_status == "REJECTED":
        raise HTTPException(status_code=403, detail="Rejected by reviewer")
    if row.review_status == "CLAIMED":
        raise HTTPException(status_code=409, detail="Already claimed")

    amount, merchant = row.amount, row.merchant

    # Atomically move APPROVED -> CLAIMED so a card can only be issued once.
    res = db.execute(
        update(Ledger)
        .where(Ledger.id == decision_id, Ledger.review_status == "APPROVED")
        .values(review_status="CLAIMED")
    )
    if res.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="Already claimed")

    if not reserve_budget(db, agent_id, amount):
        db.rollback()  # status goes back to APPROVED; can be retried later
        raise HTTPException(
            status_code=409,
            detail={"decision": "DECLINE", "reason": "EXCEEDS_MONTHLY_BUDGET_OR_FROZEN"},
        )

    try:
        credentials = issuer.create_single_use_card(
            amount_cents=to_cents(amount), currency=CARD_CURRENCY, merchant=merchant
        )
    except Exception as exc:
        db.rollback()
        logger.error("Card issuance failed on claim %s: %s", decision_id, exc)
        raise HTTPException(
            status_code=500,
            detail={"decision": "ERROR", "reason": "ISSUER_ERROR", "evidence_id": decision_id},
        )

    card_id = credentials.pop("id")
    db.execute(update(Ledger).where(Ledger.id == decision_id).values(issuer_card_id=card_id))
    db.commit()
    return {
        "decision": "ALLOW",
        "reason": "HUMAN_APPROVED",
        "evidence_id": decision_id,
        "payment_credentials": credentials,
    }


app.mount("/", StaticFiles(directory="static", html=True), name="static")

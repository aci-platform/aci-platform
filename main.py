from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, JSON
from sqlalchemy.orm import declarative_base, sessionmaker, Session
import uuid
import secrets
import hashlib

SQLALCHEMY_DATABASE_URL = "sqlite:///./aci.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

class Agent(Base):
    __tablename__ = "agents"
    id = Column(String, primary_key=True, index=True)
    name = Column(String)
    api_key_hash = Column(String, unique=True, index=True)
    max_transaction = Column(Float)
    monthly_limit = Column(Float)
    spend_this_month = Column(Float, default=0.0)
    allowed_merchants = Column(JSON)
    allowed_categories = Column(JSON)
    require_approval_over = Column(Float)
    is_frozen = Column(Boolean, default=False)

class Ledger(Base):
    __tablename__ = "ledger"
    id = Column(String, primary_key=True, index=True)
    agent_id = Column(String)
    amount = Column(Float)
    merchant = Column(String)
    category = Column(String)
    decision = Column(String)
    reason = Column(String)

Base.metadata.create_all(bind=engine)

class AgentCreate(BaseModel):
    name: str
    max_transaction: float
    monthly_limit: float
    allowed_merchants: List[str]
    allowed_categories: List[str]
    require_approval_over: float

class AuthRequest(BaseModel):
    amount: float
    merchant: str
    category: str

app = FastAPI()
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.post("/api/agents", status_code=201)
def create_agent(agent: AgentCreate, db: Session = Depends(get_db)):
    agent_id = f"agent_{uuid.uuid4().hex[:8]}"
    raw_api_key = f"aci_live_{secrets.token_urlsafe(24)}"
    db_agent = Agent(id=agent_id, name=agent.name, api_key_hash=hash_key(raw_api_key), max_transaction=agent.max_transaction, monthly_limit=agent.monthly_limit, allowed_merchants=agent.allowed_merchants, allowed_categories=agent.allowed_categories, require_approval_over=agent.require_approval_over)
    db.add(db_agent)
    db.commit()
    return {"agent_id": agent_id, "api_key": raw_api_key}

@app.get("/api/agents")
def list_agents(db: Session = Depends(get_db)):
    return db.query(Agent).all()

@app.post("/api/agents/{agent_id}/freeze")
def toggle_freeze(agent_id: str, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent:
        agent.is_frozen = not agent.is_frozen
        db.commit()
    return {"status": "ok"}

@app.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent:
        db.delete(agent)
        db.commit()
    return {"status": "deleted"}

@app.get("/api/ledger")
def get_ledger(db: Session = Depends(get_db)):
    return db.query(Ledger).order_by(Ledger.id.desc()).limit(10).all()

def process_auth(agent: Agent, request: AuthRequest, db: Session):
    if agent.is_frozen: return "DECLINE", "AGENT_FROZEN"
    if request.amount > agent.max_transaction: return "DECLINE", "EXCEEDS_TRANSACTION_LIMIT"
    if request.merchant not in agent.allowed_merchants: return "DECLINE", "MERCHANT_NOT_ALLOWED"
    if request.category not in agent.allowed_categories: return "DECLINE", "CATEGORY_NOT_ALLOWED"
    if (agent.spend_this_month + request.amount) > agent.monthly_limit: return "DECLINE", "EXCEEDS_MONTHLY_BUDGET"
    if request.amount > agent.require_approval_over: return "REVIEW", "REQUIRES_HUMAN_APPROVAL"
    
    agent.spend_this_month += request.amount
    db.commit()
    return "ALLOW", "WITHIN_POLICY"

@app.post("/v1/authorize")
def authorize_purchase(request: AuthRequest, authorization: str = Header(None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.split(" ")[1]
    agent = db.query(Agent).filter(Agent.api_key_hash == hash_key(token)).first()
    if not agent: raise HTTPException(status_code=401, detail="Invalid API Key")
    
    decision, reason = process_auth(agent, request, db)
    log = Ledger(id=f"dec_{uuid.uuid4().hex[:8]}", agent_id=agent.id, amount=request.amount, merchant=request.merchant, category=request.category, decision=decision, reason=reason)
    db.add(log)
    db.commit()
    return {"decision": decision, "reason": reason, "evidence_id": log.id}

# THIS IS THE MAGIC BUTTON ENDPOINT FOR THE DASHBOARD
@app.post("/api/agents/{agent_id}/test")
def test_purchase(agent_id: str, request: AuthRequest, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent: raise HTTPException(status_code=404)
    decision, reason = process_auth(agent, request, db)
    log = Ledger(id=f"dec_{uuid.uuid4().hex[:8]}", agent_id=agent.id, amount=request.amount, merchant=request.merchant, category=request.category, decision=decision, reason=reason)
    db.add(log)
    db.commit()
    return {"decision": decision, "reason": reason}

app.mount("/", StaticFiles(directory="static", html=True), name="static")

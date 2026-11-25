"""SQLAlchemy models. Sensitive columns (suffix ``_enc``) hold AES-256-GCM envelope ciphertext."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import DATABASE_URL

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Business(Base):
    __tablename__ = "businesses"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    sector = Column(String(80), nullable=False)
    city = Column(String(80), nullable=False)
    commercial_register_enc = Column(Text, nullable=False)
    tax_id_enc = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(200), unique=True, nullable=False, index=True)
    full_name = Column(String(200), nullable=False)
    role = Column(String(30), nullable=False)  # sme_owner | advisor | auditor
    password_hash = Column(String(300), nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=True)
    assigned_business_ids = Column(JSON, default=list)  # advisors / auditors scope
    failed_logins = Column(Integer, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True)
    business = relationship("Business")


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    invoice_number = Column(String(80), nullable=False)
    buyer_ref = Column(String(64), nullable=False, index=True)  # keyed hash of buyer tax id
    buyer_name_enc = Column(Text, nullable=False)
    buyer_tax_id_enc = Column(Text, nullable=False)
    buyer_sector = Column(String(60), nullable=False)
    amount_enc = Column(Text, nullable=False)
    currency = Column(String(3), default="EGP")
    issue_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    paid_date = Column(Date, nullable=True)
    status = Column(String(20), nullable=False)  # paid | open | financed
    source = Column(String(30), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    txn_date = Column(Date, nullable=False)
    category = Column(String(30), nullable=False)
    direction = Column(String(6), nullable=False)  # credit | debit
    description_enc = Column(Text, nullable=False)
    amount_enc = Column(Text, nullable=False)
    balance_enc = Column(Text, nullable=True)
    source_sha256 = Column(String(64), nullable=False)


class CreditScore(Base):
    __tablename__ = "credit_scores"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    score = Column(Float, nullable=False)
    grade = Column(String(2), nullable=False)
    dossier_enc = Column(Text, nullable=False)
    model_version = Column(String(40), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)


class FinancingApplication(Base):
    __tablename__ = "financing_applications"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False, index=True)
    offer_id = Column(String(40), nullable=False)
    provider = Column(String(120), nullable=False)
    product = Column(String(60), nullable=False)
    invoice_ids = Column(JSON, default=list)
    payload_enc = Column(Text, nullable=False)
    status = Column(String(40), nullable=False)  # pending_owner_approval | submitted | rejected_by_owner
    created_by = Column(String(40), nullable=False)  # agent | user:<id>
    approved_by = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    decided_at = Column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Append-only, hash-chained audit trail. Updates and deletes are blocked at ORM level."""

    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    ts = Column(String(40), nullable=False)
    actor = Column(String(120), nullable=False)
    role = Column(String(30), nullable=False)
    action = Column(String(80), nullable=False)
    business_id = Column(Integer, nullable=True, index=True)
    details = Column(JSON, default=dict)
    prev_hash = Column(String(64), nullable=False)
    entry_hash = Column(String(64), nullable=False, unique=True)


@event.listens_for(AuditLog, "before_update")
def _block_audit_update(mapper, connection, target):  # noqa: ARG001
    raise PermissionError("Audit log entries are immutable")


@event.listens_for(AuditLog, "before_delete")
def _block_audit_delete(mapper, connection, target):  # noqa: ARG001
    raise PermissionError("Audit log entries cannot be deleted")


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

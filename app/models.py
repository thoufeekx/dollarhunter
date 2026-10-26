import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey,
                        ForeignKeyConstraint, Index, Integer, Numeric, String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

JB = JSON().with_variant(JSONB(), "postgresql")
Money = Numeric(12, 2)
Qty = Numeric(12, 3)


def pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def cust_fk():
    return mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False, index=True)


def ts(**kw):
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


def opt_ts():
    return mapped_column(DateTime(timezone=True), nullable=True)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[uuid.UUID] = pk()
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(80))
    postal_prefix: Mapped[str | None] = mapped_column(String(3))
    onboarding_state: Mapped[str] = mapped_column(String(30), default="new")
    sms_consent_state: Mapped[str] = mapped_column(String(20), default="none")
    sms_consent_source: Mapped[str | None] = mapped_column(String(40))
    sms_consent_at: Mapped[datetime | None] = opt_ts()
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    preferences: Mapped[dict] = mapped_column(JB, default=dict)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = ts()


class Input(Base):
    __tablename__ = "inputs"
    __table_args__ = (UniqueConstraint("channel", "external_id"),)
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    channel: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(120))
    text: Mapped[str | None] = mapped_column(Text)
    num_media: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = ts()
    processing_status: Mapped[str] = mapped_column(String(20), default="pending")


class StoredFile(Base):
    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("id", "customer_id"),)
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    input_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("inputs.id"))
    storage_key: Mapped[str] = mapped_column(String(200), unique=True)
    detected_mime: Mapped[str] = mapped_column(String(80))
    byte_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    processing_status: Mapped[str] = mapped_column(String(20), default="pending")
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = opt_ts()
    created_at: Mapped[datetime] = ts()


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (UniqueConstraint("id", "customer_id"),
                      ForeignKeyConstraint(["file_id", "customer_id"], ["files.id", "files.customer_id"]))
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    store_name: Mapped[str | None] = mapped_column(String(120))
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3), default="CAD")
    subtotal: Mapped[Decimal | None] = mapped_column(Money)
    tax: Mapped[Decimal | None] = mapped_column(Money)
    total: Mapped[Decimal | None] = mapped_column(Money)
    warnings: Mapped[list] = mapped_column(JB, default=list)
    extraction_version: Mapped[str] = mapped_column(String(60))
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_at: Mapped[datetime | None] = opt_ts()
    created_at: Mapped[datetime] = ts()


class ReceiptLine(Base):
    __tablename__ = "receipt_lines"
    __table_args__ = (ForeignKeyConstraint(["receipt_id", "customer_id"], ["receipts.id", "receipts.customer_id"]),)
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(String(120))
    brand: Mapped[str | None] = mapped_column(String(80))
    quantity: Mapped[Decimal | None] = mapped_column(Qty)
    unit: Mapped[str | None] = mapped_column(String(30))
    pack_size: Mapped[str | None] = mapped_column(String(40))
    unit_price: Mapped[Decimal | None] = mapped_column(Money)
    line_total: Mapped[Decimal | None] = mapped_column(Money)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    corrected: Mapped[dict | None] = mapped_column(JB)


class Basket(Base):
    __tablename__ = "baskets"
    __table_args__ = (UniqueConstraint("id", "customer_id"),
                      CheckConstraint("status in ('draft','confirmed','quoted','ordered','closed')"),
                      Index("uq_one_open_basket", "customer_id", unique=True,
                            postgresql_where="status in ('draft','confirmed','quoted')"))
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    status: Mapped[str] = mapped_column(String(12), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    budget: Mapped[Decimal | None] = mapped_column(Money)
    instructions: Mapped[list] = mapped_column(JB, default=list)
    copied_from_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = ts()
    closed_at: Mapped[datetime | None] = opt_ts()


class BasketLine(Base):
    __tablename__ = "basket_lines"
    __table_args__ = (UniqueConstraint("id", "customer_id"),
                      ForeignKeyConstraint(["basket_id", "customer_id"], ["baskets.id", "baskets.customer_id"]))
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    basket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(120))
    sku: Mapped[str | None] = mapped_column(String(40))
    quantity: Mapped[Decimal | None] = mapped_column(Qty)
    unit: Mapped[str | None] = mapped_column(String(30))
    brand_pref: Mapped[str | None] = mapped_column(String(80))
    size_pref: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(20), default="text")
    source_ref: Mapped[str | None] = mapped_column(String(80))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    substitution_status: Mapped[str] = mapped_column(String(20), default="none")
    note: Mapped[str | None] = mapped_column(String(200))
    keywords: Mapped[list | None] = mapped_column(JB)  # store wording to search for, e.g. ["bathroom tissue"]
    details: Mapped[str | None] = mapped_column(String(200))  # what this customer means, written by the agent


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[uuid.UUID] = pk()
    store: Mapped[str] = mapped_column(String(80))
    sku: Mapped[str] = mapped_column(String(40), index=True)
    product: Mapped[str] = mapped_column(String(120))
    brand: Mapped[str | None] = mapped_column(String(80))
    unit: Mapped[str] = mapped_column(String(30))
    pack_size: Mapped[str] = mapped_column(String(60))
    equivalence_key: Mapped[str] = mapped_column(String(80), index=True)
    price: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="CAD")
    kind: Mapped[str] = mapped_column(String(12), default="offer")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    availability: Mapped[str] = mapped_column(String(20), default="in_stock")
    source_type: Mapped[str] = mapped_column(String(30))
    source_ref: Mapped[str | None] = mapped_column(String(200))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=True)


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (UniqueConstraint("id", "customer_id"),
                      ForeignKeyConstraint(["basket_id", "customer_id"], ["baskets.id", "baskets.customer_id"]))
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    basket_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    basket_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="open")
    lines: Mapped[list] = mapped_column(JB)
    charges: Mapped[dict] = mapped_column(JB)
    baseline: Mapped[dict] = mapped_column(JB)
    verification_status: Mapped[str] = mapped_column(String(20))
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gross_savings: Mapped[Decimal] = mapped_column(Money, default=0)
    fee: Mapped[Decimal] = mapped_column(Money, default=0)
    net_savings: Mapped[Decimal] = mapped_column(Money, default=0)
    total: Mapped[Decimal | None] = mapped_column(Money)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = ts()


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("id", "customer_id"), UniqueConstraint("quote_id", "idempotency_key"),
                      ForeignKeyConstraint(["quote_id", "customer_id"], ["quotes.id", "quotes.customer_id"]))
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    quote_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    quote_basket_version: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(80))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = opt_ts()
    consent_snapshot: Mapped[dict] = mapped_column(JB, default=dict)
    created_at: Mapped[datetime] = ts()


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("quote_id"), UniqueConstraint("id", "customer_id"),
                      CheckConstraint("mode = 'simulated'"),
                      ForeignKeyConstraint(["quote_id", "customer_id"], ["quotes.id", "quotes.customer_id"]),
                      ForeignKeyConstraint(["approval_id", "customer_id"], ["approvals.id", "approvals.customer_id"]))
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    quote_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(String(12), default="simulated")
    status: Mapped[str] = mapped_column(String(20), default="simulated_placed")
    delivery: Mapped[dict] = mapped_column(JB, default=dict)
    created_at: Mapped[datetime] = ts()


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (UniqueConstraint("order_id", "entry_type"),
                      CheckConstraint("entry_type in ('completion','reversal')"),
                      ForeignKeyConstraint(["order_id", "customer_id"], ["orders.id", "orders.customer_id"]))
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    entry_type: Mapped[str] = mapped_column(String(12))
    gross: Mapped[Decimal] = mapped_column(Money)
    fee: Mapped[Decimal] = mapped_column(Money)
    net: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(12), default="simulated")
    reverses_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ledger_entries.id"))
    created_at: Mapped[datetime] = ts()


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JB, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(12), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_expires_at: Mapped[datetime | None] = opt_ts()
    run_after: Mapped[datetime] = ts()
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = ts()
    finished_at: Mapped[datetime | None] = opt_ts()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_claim", "direction", "status"),)
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    direction: Mapped[str] = mapped_column(String(3), default="out")
    body: Mapped[str] = mapped_column(Text)
    segments: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(30), default="reply")
    status: Mapped[str] = mapped_column(String(12), default="queued")
    provider: Mapped[str | None] = mapped_column(String(12))
    provider_sid: Mapped[str | None] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True)
    allow_when_opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = ts()
    sent_at: Mapped[datetime | None] = opt_ts()


class AccessToken(Base):
    __tablename__ = "access_tokens"
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID] = cust_fk()
    purpose: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = opt_ts()
    created_at: Mapped[datetime] = ts()


class Event(Base):
    __tablename__ = "events"
    id: Mapped[uuid.UUID] = pk()
    customer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("customers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    data: Mapped[dict] = mapped_column(JB, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = ts()


class CacheEntry(Base):
    """Shared cache for public data (store lists, flyer searches). Never holds customer data."""
    __tablename__ = "cache_entries"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    data: Mapped[dict] = mapped_column(JB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class FlyerItem(Base):
    """This week's flyer items for a postal area, downloaded in bulk so lookups are local and instant."""
    __tablename__ = "flyer_items"
    __table_args__ = (Index("ix_flyer_items_area", "fsa", "valid_to"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # "<fsa>:<flipp item id>"
    fsa: Mapped[str] = mapped_column(String(3))
    flyer_id: Mapped[str] = mapped_column(String(20), index=True)
    store: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    brand: Mapped[str | None] = mapped_column(String(120))
    price: Mapped[Decimal] = mapped_column(Money)
    regular: Mapped[Decimal | None] = mapped_column(Money)
    valid_from: Mapped[datetime | None] = opt_ts()
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = ts()

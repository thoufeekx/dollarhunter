from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, Field


class ReceiptLineOut(BaseModel):
    raw_text: str
    name: str | None = None
    brand: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    pack_size: str | None = None
    unit_price: Decimal | None = None
    line_total: Decimal | None = None
    confidence: float = Field(0.0, ge=0, le=1)
    needs_review: bool = False


class ReceiptOut(BaseModel):
    store_name: str | None = None
    purchased_at: str | None = None
    currency: str = "CAD"
    lines: list[ReceiptLineOut] = []
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    warnings: list[str] = []
    is_mock: bool = False
    extraction_version: str = ""


class ParsedItem(BaseModel):
    name: str
    sku: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    question: str | None = None
    confidence: float = 0.9


class Instruction(BaseModel):
    type: str
    text: str


class ParsedList(BaseModel):
    items: list[ParsedItem] = []
    instructions: list[Instruction] = []
    removals: list[str] = []


class SendResult(BaseModel):
    status: str
    provider: str
    sid: str | None = None


class ReceiptExtractor(Protocol):
    def extract(self, data: bytes, mime: str) -> ReceiptOut: ...


class ListParser(Protocol):
    def parse(self, text: str) -> ParsedList: ...


class Transcriber(Protocol):
    is_mock: bool
    def transcribe(self, data: bytes, mime: str) -> str: ...


class PriceSource(Protocol):
    def offers_for(self, session, sku: str) -> list: ...


class SmsProvider(Protocol):
    name: str
    def send(self, to: str, body: str, dedupe_key: str) -> SendResult: ...


class OrderProvider(Protocol):
    mode: str
    def place(self, quote_id: str) -> dict: ...

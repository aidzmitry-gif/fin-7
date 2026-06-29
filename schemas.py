"""Pydantic-схемы модуля Finance."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class PaymentCreate(BaseModel):
    ref: str
    amount: float = 0
    status: str = "pending"
    kind: str = "receivable"
    due_date: date | None = None
    deal_id: int | None = None
    counterparty_ref: str | None = None
    account_id: int | None = None


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ref: str
    amount: float
    status: str
    kind: str = "receivable"
    due_date: date | None = None
    paid_at: datetime | None = None
    deal_id: int | None = None
    counterparty_ref: str | None = None
    account_id: int | None = None
    # Вычисляемые поля (заполняются в роутере):
    outstanding: float | None = None  # остаток к поступлению (amount − sum allocations)
    is_overdue: bool | None = None  # status in (pending, partial) and due_date < today


class StatusUpdate(BaseModel):
    status: str


class AllocationCreate(BaseModel):
    amount: float


class AllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    payment_id: int
    amount: float
    allocated_at: datetime


class PaymentDetail(PaymentOut):
    allocations: list[AllocationOut] = []


# ───────────────────────── Р4: банковские счета ─────────────────────────


class BankAccountCreate(BaseModel):
    code: str
    title: str
    currency: str = "BYN"
    opening_balance: float = 0
    opening_at: date | None = None


class BankAccountUpdate(BaseModel):
    title: str | None = None
    is_active: bool | None = None
    opening_balance: float | None = None
    opening_at: date | None = None


class BankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    title: str
    currency: str
    opening_balance: float
    opening_at: date | None = None
    is_active: bool


# ───────────────────────── Р5: P&L ─────────────────────────


class PnlOut(BaseModel):
    """Отчёт о прибылях и убытках за период (BYN, строки для точности)."""

    period_from: str
    period_to: str
    revenue: str
    cogs: str
    freight: str
    payroll: str
    opex: str
    tax: str
    bank_fee: str
    operating_profit: str

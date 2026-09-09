"""Pydantic-схемы модуля Finance.

Деньги на API-границе — **строки** (не float): float дрейфует копейки на собственнике
(приоритет №1 PLATFORM.md). Единая точка квантования — ``money_str`` (Decimal → "0.01").
``MoneyStr``/``OptMoneyStr`` (``BeforeValidator``) принимают ORM ``Decimal``, JSON-число
(фронт шлёт number на allocation/bank-account) и уже-строку — и всегда отдают строку BYN.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict


def money_str(value: object) -> str:
    """Денежное значение (Decimal/float/int/str) → строка BYN, 2 знака.

    Единая точка квантования для API-границы Finance — используется и схемами, и компут-
    модулями (summary/aging/cashflow/margin/cost_center/reconcile), чтобы копейки не расходились.
    """
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def _opt_money_str(value: object) -> str | None:
    return None if value is None else money_str(value)


# str на API; BeforeValidator конвертирует вход (Decimal ORM / JSON-число / строка) → строку BYN.
MoneyStr = Annotated[str, BeforeValidator(money_str)]
OptMoneyStr = Annotated[str | None, BeforeValidator(_opt_money_str)]


class PaymentCreate(BaseModel):
    ref: str
    amount: MoneyStr = "0.00"
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
    amount: MoneyStr
    status: str
    kind: str = "receivable"
    due_date: date | None = None
    paid_at: datetime | None = None
    deal_id: int | None = None
    counterparty_ref: str | None = None
    account_id: int | None = None
    # Вычисляемые поля (заполняются в роутере):
    outstanding: OptMoneyStr = None  # остаток к поступлению (amount − sum allocations)
    is_overdue: bool | None = None  # status in (pending, partial) and due_date < today


class StatusUpdate(BaseModel):
    status: str


class AllocationCreate(BaseModel):
    amount: MoneyStr


class AllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    payment_id: int
    amount: MoneyStr
    allocated_at: datetime


class PaymentDetail(PaymentOut):
    allocations: list[AllocationOut] = []


# ───────────────────────── Р4: банковские счета ─────────────────────────


class BankAccountCreate(BaseModel):
    code: str
    title: str
    currency: str = "BYN"
    opening_balance: MoneyStr = "0.00"
    opening_at: date | None = None


class BankAccountUpdate(BaseModel):
    title: str | None = None
    is_active: bool | None = None
    opening_balance: OptMoneyStr = None
    opening_at: date | None = None


class BankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    title: str
    currency: str
    opening_balance: MoneyStr
    opening_at: date | None = None
    is_active: bool


# ───────────────────────── Банк: входящие зачисления (Альфа) ─────────────────────────


class BankTxOut(BaseModel):
    """Строка ledger банковских зачислений (для очереди «разобрать вручную»)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    ext_id: str
    occurred_on: date | None = None
    amount: MoneyStr
    currency: str = "BYN"
    payer_unp: str | None = None
    payer_name: str | None = None
    purpose: str | None = None
    account_code: str | None = None
    match_status: str
    note: str | None = None
    payment_id: int | None = None


class BankManualMatch(BaseModel):
    """Ручная привязка зачисления к счёту (очередь «разобрать вручную»)."""

    payment_id: int


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

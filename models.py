"""ORM-модели модуля Finance (схема ``finance.*``)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class Payment(Base):
    """Платёж: назначение (сделка/документ), сумма, статус.

    Жизненный цикл (``status``, свободная строка — не PG-enum): ``planned`` →
    ``pending`` → ``partial`` → ``paid``. «``overdue``» в статусе НЕ хранится —
    вычисляется на чтении: ``status in (pending, partial) and due_date < today``.
    """

    __tablename__ = "payment"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    # kind проводки: receivable — счёт к получению (доход); freight — расход на перевозку;
    # freight_refund — возврат переплаты перевозчиком (отрицат. сумма); landed — себестоимость
    # прихода из закупок (расход). Свободная строка (не PG-enum) — новые виды без миграции.
    kind: Mapped[str] = mapped_column(String(32), default="receivable", server_default="receivable")
    # сроки lifecycle (0061): plan/факт даты — none-ok, для honest-empty
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # провенанс связи (для маржи by-deal/by-counterparty)
    deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counterparty_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # центр затрат (0063): свободная строка из захардкоженного справочника cost_center.py
    cost_center: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # мультивалюта (0063): amount всегда в BYN (после конвертации с FX-буфером);
    # amount_orig + currency — оригинал из payload (None при BYN-в-источнике)
    currency: Mapped[str] = mapped_column(String(3), default="BYN", server_default="BYN")
    amount_orig: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # банк-счёт (0075, Р4): None = «основной кэш» (для совместимости с лайфом до Р4)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance.bank_account.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class BankAccount(Base):
    """Банковский счёт / касса (Р4 — Платёжный календарь, 0075).

    Свободный справочник под управленческий учёт денежных средств. ``opening_balance`` —
    начальное сальдо на ``opening_at`` (фикс при заведении), от него считаем running-баланс
    как ``opening + Σ paid receivable − Σ paid expenses (с фильтром account_id=this)``.
    Когда подключится `OneCGateway.fetch_bank_balance` (Р6) — opening сверим с 1С.

    ``code`` — компактный идентификатор для UI/событий («main», «cny», «cash»); ``currency``
    — валюта счёта (BYN/USD/CNY/EUR/RUB). Активность — флаг ``is_active`` (не удаляем,
    выводим из выбора UI).
    """

    __tablename__ = "bank_account"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3), default="BYN", server_default="BYN")
    opening_balance: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), default=Decimal("0"), server_default="0"
    )
    opening_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Integer, default=1, server_default="1"
    )  # 0/1 — SQLite-совместимо без BOOLEAN
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PaymentAllocation(Base):
    """Частичное поступление по платежу (0061).

    ``sum(allocations.amount) >= payment.amount`` → статус ``paid``; ``0 < sum < amount``
    → ``partial``; ``sum == 0`` → ``pending``/``planned`` без изменения.
    """

    __tablename__ = "payment_allocation"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(
        ForeignKey("finance.payment.id", ondelete="CASCADE"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    allocated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

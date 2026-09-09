"""Проведение поступления по платежу — единая точка (роут + банк-импорт).

Выделено из ``routes.create_allocation``, чтобы ручной ввод (POST /allocations) и
авто-проводка банковского зачисления (``bank_ingest``) шли ОДНИМ путём: одинаковый
пересчёт статуса и одни и те же события ``finance.payment.received`` / ``.paid``.

Транзакцию НЕ коммитим — граница у вызывающего (роут / цикл импорта).
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment, PaymentAllocation


async def sum_allocations(session: AsyncSession, payment_id: int) -> Decimal:
    """Σ поступлений по платежу (Decimal, 0 при отсутствии)."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
                PaymentAllocation.payment_id == payment_id
            )
        )
    ).scalar_one()
    return Decimal(str(total))


async def apply_allocation(
    session: AsyncSession, event_bus, payment: Payment, amount: Decimal
) -> PaymentAllocation:
    """Зафиксировать поступление ``amount`` по ``payment`` + пересчёт статуса + события.

    Семантика (0061 / FIN-C3):
      - на **каждое** поступление (вкл. частичное) → ``finance.payment.received`` (office его ждёт);
      - Σ ≥ amount → статус ``paid`` + ``finance.payment.paid`` (полное закрытие);
      - 0 < Σ < amount → ``partial``.
    Деньги — **строкой** в событии (float дрейфует копейки собственника, FIN-A2).
    """
    alloc = PaymentAllocation(payment_id=payment.id, amount=amount)
    session.add(alloc)
    await session.flush()
    total = await sum_allocations(session, payment.id)
    target = Decimal(str(payment.amount))
    outstanding_after = target - total
    event_bus.emit(
        session,
        "finance.payment.received",
        {
            "ref": payment.ref,
            "amount": str(amount),
            "entity_ref": f"payment:{payment.id}",
            "deal_id": payment.deal_id,
            "counterparty_ref": payment.counterparty_ref,
            "outstanding": str(outstanding_after if outstanding_after > 0 else Decimal("0")),
        },
    )
    if total >= target:
        if payment.status != "paid":
            payment.status = "paid"
            payment.paid_at = datetime.now(UTC)
            event_bus.emit(
                session,
                "finance.payment.paid",
                {"ref": payment.ref, "deal_id": payment.deal_id, "entity_ref": f"payment:{payment.id}"},
            )
    elif total > 0:
        payment.status = "partial"
    return alloc


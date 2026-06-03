"""HTTP-API модуля Finance. Монтируется под префиксом ``/finance``."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from modules.finance.models import Payment
from modules.finance.schemas import PaymentCreate, PaymentOut, StatusUpdate

router = APIRouter(tags=["finance"])


@router.get("/payments", response_model=list[PaymentOut])
async def list_payments(session: AsyncSession = Depends(get_session)):
    """Платежи."""
    return (await session.execute(select(Payment).order_by(Payment.id.desc()))).scalars().all()


@router.post("/payments", response_model=PaymentOut, status_code=201)
async def create_payment(payload: PaymentCreate, session: AsyncSession = Depends(get_session)):
    """Зафиксировать платёж."""
    obj = Payment(ref=payload.ref, amount=Decimal(str(payload.amount)), status=payload.status)
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


@router.patch("/payments/{payment_id}", response_model=PaymentOut)
async def update_payment(
    payment_id: int,
    payload: StatusUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сменить статус платежа. При ``paid`` — документ-счёт помечается оплаченным (finance → sales)."""
    obj = await session.get(Payment, payment_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    obj.status = payload.status
    if payload.status == "paid":
        core.event_bus.emit(
            session, "finance.payment.paid", {"ref": obj.ref, "entity_ref": f"payment:{obj.id}"}
        )
    await session.commit()
    await session.refresh(obj)
    return obj

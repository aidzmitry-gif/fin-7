"""HTTP-API модуля Finance. Монтируется под префиксом ``/finance``."""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.deps import get_session
from modules.finance.models import Payment
from modules.finance.schemas import PaymentCreate, PaymentOut

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

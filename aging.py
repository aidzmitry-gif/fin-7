"""AR/AP по корзинам срока (Aging report).

Дебиторка (AR) = ``receivable`` с outstanding > 0; кредиторка (AP) = расходные kind
(``freight``/``landed``) с outstanding > 0. ``freight_refund`` — кредит, в AP не идёт.
Корзины по (today − due_date): ``current`` (срок не наступил), ``1-30``, ``31-60``,
``61-90``, ``90+``. Строки без due_date — отдельная корзина ``no_due`` (НЕ валим в
current молча, помечаем явно — honest-empty).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment, PaymentAllocation
from modules.finance.schemas import money_str

AR_KINDS = ("receivable",)
AP_KINDS = ("freight", "landed")
BUCKETS = ("current", "1-30", "31-60", "61-90", "90+", "no_due")


def _bucket(due: date | None, today: date) -> str:
    if due is None:
        return "no_due"
    days_overdue = (today - due).days
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


async def _payments_outstanding(
    session: AsyncSession, kinds: tuple[str, ...]
) -> list[tuple[Payment, Decimal]]:
    """Платежи нужных видов с непокрытым остатком (outstanding > 0)."""
    rows = (
        await session.execute(
            select(Payment).where(
                Payment.kind.in_(kinds), Payment.status.in_(("planned", "pending", "partial"))
            )
        )
    ).scalars().all()
    if not rows:
        return []
    sums = dict(
        (
            await session.execute(
                select(PaymentAllocation.payment_id, func.sum(PaymentAllocation.amount))
                .where(PaymentAllocation.payment_id.in_([p.id for p in rows]))
                .group_by(PaymentAllocation.payment_id)
            )
        ).all()
    )
    out = []
    for p in rows:
        allocated = Decimal(str(sums.get(p.id, 0)))
        outstanding = Decimal(str(p.amount)) - allocated
        if outstanding > 0:
            out.append((p, outstanding))
    return out


async def _side(
    session: AsyncSession, kinds: tuple[str, ...], today: date
) -> dict:
    """Свернуть платежи в корзины + total."""
    buckets = {b: Decimal("0") for b in BUCKETS}
    for p, outstanding in await _payments_outstanding(session, kinds):
        buckets[_bucket(p.due_date, today)] += outstanding
    total = sum(buckets.values(), Decimal("0"))
    return {
        "buckets": {b: money_str(buckets[b]) for b in BUCKETS},
        "total": money_str(total),
    }


async def aging_buckets(session: AsyncSession, today: date | None = None) -> dict:
    today = today or date.today()
    return {
        "as_of": today.isoformat(),
        "currency": "BYN",
        "ar": await _side(session, AR_KINDS, today),
        "ap": await _side(session, AP_KINDS, today),
    }

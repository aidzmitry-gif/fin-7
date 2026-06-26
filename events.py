"""Реакции модуля Finance на события других модулей (через шину, §2.5).

Finance не знает о sales напрямую — реагирует на доменное событие из шины и
пишет в свою таблицу через контекст доставки (``EventContext``).
"""
from __future__ import annotations

from decimal import Decimal

from modules.finance.models import Payment


async def on_document_posted(payload: dict, ctx) -> None:
    """Счёт записан в 1С → создаём платёж к оплате (sales → finance)."""
    if payload.get("kind") != "invoice" or ctx is None:
        return
    amount = Decimal(str(payload.get("amount", 0)))
    ctx.session.add(Payment(ref=payload.get("number", ""), amount=amount, status="pending"))
    ctx.services.event_bus.emit(
        ctx.session,
        "finance.payment.created",
        {
            "ref": payload.get("number"),
            "amount": float(amount),
            "deal_id": payload.get("deal_id"),
            "entity_ref": payload.get("entity_ref"),
        },
    )


async def on_freight_cost(payload: dict, ctx) -> None:
    """Доставка завершена → расход на перевозку (logistics → finance).

    Пишем платёж ``kind="freight"`` (расход), чтобы доход (счета) и фрахт можно было
    развести при подсчёте валовой прибыли. Нулевой/пустой тариф игнорируем.
    """
    if ctx is None:
        return
    amount = Decimal(str(payload.get("amount", 0)))
    if amount <= 0:
        return
    ref = payload.get("ref") or payload.get("entity_ref") or ""
    ctx.session.add(Payment(ref=f"freight:{ref}", amount=amount, status="pending", kind="freight"))

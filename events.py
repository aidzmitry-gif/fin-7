"""Реакции модуля Finance на события других модулей (через шину, §2.5).

Finance не знает о sales напрямую — реагирует на доменное событие из шины и
пишет в свою таблицу через контекст доставки (``EventContext``).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from modules.finance.fx import BASE as BASE_CCY
from modules.finance.fx import to_byn
from modules.finance.models import Payment


def _parse_due_date(raw) -> date | None:
    """Принять ISO-строку или date; вернуть date или None (honest-empty)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None  # ponytail: подгрузим валидацию когда продажи начнут слать срок


async def on_document_posted(payload: dict, ctx) -> None:
    """Счёт записан в 1С → создаём платёж к оплате (sales → finance).

    Лайфсайкл (0061): ``due_date`` из payload (по умолчанию None — honest-empty),
    ``deal_id``/``counterparty_ref`` — провенанс связи для маржи by-deal/by-counterparty.
    """
    if payload.get("kind") != "invoice" or ctx is None:
        return
    amount = Decimal(str(payload.get("amount", 0)))
    ctx.session.add(
        Payment(
            ref=payload.get("number", ""),
            amount=amount,
            status="pending",
            due_date=_parse_due_date(payload.get("due_date")),
            deal_id=payload.get("deal_id"),
            counterparty_ref=payload.get("counterparty_ref"),
        )
    )
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
    currency = (payload.get("currency") or BASE_CCY).upper()
    amount_byn = to_byn(amount, currency)
    ctx.session.add(
        Payment(
            ref=f"freight:{ref}",
            amount=amount_byn,
            amount_orig=amount if currency != BASE_CCY else None,
            currency=currency,
            status="pending",
            kind="freight",
            deal_id=payload.get("deal_id"),
            counterparty_ref=payload.get("counterparty_ref"),
        )
    )


async def on_freight_refund(payload: dict, ctx) -> None:
    """Аудит счёта перевозчика выявил переплату → возврат (logistics → finance).

    Логистика эмитит ``logistics.freight.audit_refund`` при ``variance > 0`` (счёт
    перевозчика больше тарифа). Это деньги к возврату — кредит против расхода на фрахт,
    поэтому пишем ``kind="freight_refund"`` с **отрицательной** суммой: тогда
    ``sum(amount)`` по фрахт-платежам даёт чистый фрахт, а ``kind`` хранит аудит-след.
    Нулевую/пустую сумму игнорируем.
    """
    if ctx is None:
        return
    amount = Decimal(str(payload.get("amount", 0)))
    if amount <= 0:
        return
    ref = payload.get("entity_ref") or payload.get("shipment_code") or ""
    ctx.session.add(
        Payment(
            ref=f"freight_refund:{ref}",
            amount=-amount,
            status="pending",
            kind="freight_refund",
            counterparty_ref=payload.get("counterparty_ref"),
        )
    )


async def on_landed_cost(payload: dict, ctx) -> None:
    """Закупка зафиксировала себестоимость прихода → проводка-затрата (procurement → finance).

    Подписка на ``procurement.landed_cost.calculated`` (Горизонт 2 — закупки пока его НЕ
    эмитят; потребитель готов заранее, проекция включится сама, как только событие пойдёт).
    Себестоимость прихода = расход (``kind="landed"``) для подсчёта фактической маржи:
    выручка − landed − фрахт. Сумму берём готовой (``amount``) либо ``unit×qty``. Нуль игнорим.
    """
    if ctx is None:
        return
    # Закупки эмитят либо ``amount``, либо ``total_landed_byn`` (предпочтительнее), либо
    # ``unit_landed_cost_byn × qty``. Берём первое доступное; нулевое — игнорим (honest-empty).
    amount = Decimal(str(payload.get("amount") or 0))
    if amount <= 0:
        amount = Decimal(str(payload.get("total_landed_byn") or 0))
    if amount <= 0:
        amount = Decimal(str(payload.get("unit_landed_cost_byn") or 0)) * Decimal(
            str(payload.get("qty") or 0)
        )
    if amount <= 0:
        return
    ref = payload.get("entity_ref") or payload.get("sku_code") or ""
    currency = (payload.get("currency") or BASE_CCY).upper()
    amount_byn = to_byn(amount, currency)
    ctx.session.add(
        Payment(
            ref=f"landed:{ref}",
            amount=amount_byn,
            amount_orig=amount if currency != BASE_CCY else None,
            currency=currency,
            status="pending",
            kind="landed",
            deal_id=payload.get("deal_id"),
            counterparty_ref=payload.get("counterparty_ref"),
        )
    )

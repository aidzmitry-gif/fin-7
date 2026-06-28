"""Операционная сводка финансов: фактическая маржа и касса (ДДС-lite).

Считаем ТОЛЬКО по фактам-проводкам (``finance.payment``), агрегируя по ``kind``:
- ``receivable`` — выручка (счета к получению), деньги «в плюс»;
- ``freight`` — расход на перевозку;
- ``freight_refund`` — возврат переплаты перевозчиком (хранится отрицательной суммой —
  кредит против фрахта);
- ``landed`` — себестоимость прихода из закупок (расход);
- ``claim_refund`` — компенсация от поставщика по урегулированной претензии (приток,
  УЖЕ в BYN, положительная сумма; уменьшает чистые landed-затраты);
- ``po_planned`` — планируемый отток по выписанному PO (НЕ в маржу, только в cashflow).

Фактическая маржа = выручка − (landed − claim_refund) − чистый фрахт. Это про ФАКТ
(уже выставленные счета и понесённые затраты), не про установку цены — методика цены
заблокирована отдельно и здесь не нужна. Где данных нет — отдаём нули/None (honest-empty).

Ledger/НДС/ЭСЧФ остаются в 1С — здесь только операционный срез движения денег.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment

_KINDS = ("receivable", "freight", "freight_refund", "landed", "claim_refund", "po_planned")


async def _sum_by_kind(session: AsyncSession) -> dict[str, Decimal]:
    """Сумма ``amount`` по каждому ``kind`` (отсутствующий kind = 0)."""
    rows = (
        await session.execute(
            select(Payment.kind, func.coalesce(func.sum(Payment.amount), 0)).group_by(Payment.kind)
        )
    ).all()
    by_kind = {kind: Decimal("0") for kind in _KINDS}
    for kind, total in rows:
        by_kind[kind] = Decimal(str(total))
    return by_kind


async def _paid_receivable(session: AsyncSession) -> Decimal:
    """Сумма оплаченных счетов (``receivable`` со статусом ``paid``) — реальные поступления."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.kind == "receivable", Payment.status == "paid"
            )
        )
    ).scalar_one()
    return Decimal(str(total))


async def finance_summary(session: AsyncSession) -> dict:
    """Сводка: маржа по фактам + касса (ДДС-lite). Все суммы — ``float`` (BYN) на границе API."""
    by_kind = await _sum_by_kind(session)
    paid_recv = await _paid_receivable(session)

    revenue = by_kind["receivable"]
    freight = by_kind["freight"]
    refund = by_kind["freight_refund"]  # отрицательная (кредит против фрахта)
    landed = by_kind["landed"]
    claim_refund = by_kind["claim_refund"]  # положительная (компенсация поставщика → уменьшает landed)
    # po_planned — НЕ в фактическую маржу (только cashflow); читаем для отдельного вывода

    net_freight = freight + refund  # возврат уменьшает расход на фрахт
    net_landed = landed - claim_refund  # компенсация по претензии уменьшает чистый landed
    gross = revenue - net_landed - net_freight
    # маржа в % — только когда есть выручка (иначе делёж на ноль → честный None)
    pct = float(gross / revenue * 100) if revenue > 0 else None

    # касса (ДДС-lite): поступления (оплаченные счета + возвраты фрахта) − расходы (фрахт+landed)
    refund_inflow = -refund  # хранится отрицательной → приток = модуль
    inflow = paid_recv + refund_inflow
    outflow = freight + landed
    net_cash = inflow - outflow

    f = lambda d: float(d)  # noqa: E731 — узкий конвертер Decimal→float на границе ответа
    return {
        "currency": "BYN",
        "margin": {
            "revenue": f(revenue),
            "landed": f(net_landed),  # уже с учётом claim_refund
            "landed_gross": f(landed),  # для UI — справочно «грязный» landed
            "claim_refund": f(claim_refund),
            "freight": f(net_freight),
            "gross": f(gross),
            "pct": pct,
        },
        "cash": {
            "inflow": f(inflow),
            "outflow": f(outflow),
            "net": f(net_cash),
            "received": f(paid_recv),
            "pending_receivable": f(revenue - paid_recv),
            "freight_refund": f(refund_inflow),
        },
        "costs": [
            {"kind": "landed", "label": "Себестоимость (landed)", "amount": f(landed)},
            {"kind": "freight", "label": "Фрахт", "amount": f(freight)},
            {"kind": "freight_refund", "label": "Возврат фрахта", "amount": f(refund)},
            {"kind": "claim_refund", "label": "Компенсация по претензии", "amount": f(claim_refund)},
        ],
    }

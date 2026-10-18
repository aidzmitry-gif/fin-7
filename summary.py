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

from modules.finance.models import LANDED_PROJECTION_PREFIX, Payment
from modules.finance.schemas import money_str

_KINDS = ("receivable", "freight", "freight_refund", "landed", "claim_refund", "po_planned")


async def _sum_by_kind(session: AsyncSession, *, organization_id: int) -> dict[str, Decimal]:
    """Сумма ``amount`` по каждому ``kind`` (отсутствующий kind = 0)."""
    if type(organization_id) is not int or organization_id <= 0:
        raise ValueError("organization_id must be a positive integer")
    rows = (
        await session.execute(
            select(Payment.kind, func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.organization_id == organization_id).group_by(Payment.kind)
        )
    ).all()
    by_kind = {kind: Decimal("0") for kind in _KINDS}
    for kind, total in rows:
        by_kind[kind] = Decimal(str(total))
    return by_kind


async def _paid_receivable(session: AsyncSession, *, organization_id: int) -> Decimal:
    """Сумма оплаченных счетов (``receivable`` со статусом ``paid``) — реальные поступления."""
    if type(organization_id) is not int or organization_id <= 0:
        raise ValueError("organization_id must be a positive integer")
    total = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.organization_id == organization_id,
                Payment.kind == "receivable", Payment.status == "paid"
            )
        )
    ).scalar_one()
    return Decimal(str(total))


async def finance_summary(session: AsyncSession, *, organization_id: int) -> dict:
    """Сводка: маржа по фактам + касса (ДДС-lite). Money — ``str`` BYN; ``pct`` — процент (float)."""
    by_kind = await _sum_by_kind(session, organization_id=organization_id)
    paid_recv = await _paid_receivable(session, organization_id=organization_id)

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

    # Legacy cash semantics are retained; the new verified PO cost is not cash.
    refund_inflow = -refund  # хранится отрицательной → приток = модуль
    inflow = paid_recv + refund_inflow
    cost_only = Decimal(str((await session.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.organization_id == organization_id, Payment.kind == "landed",
            Payment.entity_ref.startswith(LANDED_PROJECTION_PREFIX),
        )
    )).scalar_one()))
    outflow = freight + landed - cost_only
    net_cash = inflow - outflow

    # money — строкой BYN (см. money_str); pct — процент, НЕ деньги, остаётся float
    return {
        "currency": "BYN",
        "margin": {
            "revenue": money_str(revenue),
            "landed": money_str(net_landed),  # уже с учётом claim_refund
            "landed_gross": money_str(landed),  # для UI — справочно «грязный» landed
            "claim_refund": money_str(claim_refund),
            "freight": money_str(net_freight),
            "gross": money_str(gross),
            "pct": pct,
        },
        "cash": {
            "inflow": money_str(inflow),
            "outflow": money_str(outflow),
            "net": money_str(net_cash),
            "received": money_str(paid_recv),
            "pending_receivable": money_str(revenue - paid_recv),
            "freight_refund": money_str(refund_inflow),
        },
        "costs": [
            {"kind": "landed", "label": "Себестоимость (landed)", "amount": money_str(landed)},
            {"kind": "freight", "label": "Фрахт", "amount": money_str(freight)},
            {"kind": "freight_refund", "label": "Возврат фрахта", "amount": money_str(refund)},
            {
                "kind": "claim_refund",
                "label": "Компенсация по претензии",
                "amount": money_str(claim_refund),
            },
        ],
    }

"""Фактическая маржа по сделкам и контрагентам.

Группируем проводки по ``deal_id`` / ``counterparty_ref`` и считаем:
- выручка = sum(``receivable``);
- landed = sum(``landed``);
- net_freight = sum(``freight`` + ``freight_refund``) — возврат уменьшает фрахт;
- gross = revenue − landed − net_freight;
- pct = gross/revenue, ``None`` если выручки нет (honest-empty).

Группа без deal_id/counterparty_ref — отдельная позиция «не атрибутировано»
(чтобы не размазывать неатрибутированные деньги по чужим строкам).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment

UNATTRIBUTED_DEAL = "_unattributed_"
UNATTRIBUTED_CP = "_unattributed_"


def _row(key, agg: dict[str, Decimal]) -> dict:
    revenue = agg["receivable"]
    landed = agg["landed"]
    net_freight = agg["freight"] + agg["freight_refund"]
    gross = revenue - landed - net_freight
    pct = float(gross / revenue * 100) if revenue > 0 else None
    return {
        "key": key,
        "revenue": float(revenue),
        "landed": float(landed),
        "freight": float(net_freight),
        "gross": float(gross),
        "pct": pct,
    }


async def _grouped(session: AsyncSession, group_field) -> list[dict]:
    rows = (await session.execute(select(Payment))).scalars().all()
    by: dict[object, dict[str, Decimal]] = {}
    for p in rows:
        key = getattr(p, group_field) if getattr(p, group_field) not in (None, "") else None
        slot = by.setdefault(
            key,
            {
                "receivable": Decimal("0"),
                "landed": Decimal("0"),
                "freight": Decimal("0"),
                "freight_refund": Decimal("0"),
            },
        )
        if p.kind in slot:
            slot[p.kind] += Decimal(str(p.amount))
    result = [_row(k if k is not None else None, v) for k, v in by.items()]
    # сортируем по убыванию валовой прибыли; неатрибутированные в конец независимо от gross
    result.sort(key=lambda r: (r["key"] is None, -r["gross"]))
    return result


async def margin_by_deal(session: AsyncSession) -> dict:
    rows = await _grouped(session, "deal_id")
    return {"currency": "BYN", "items": rows}


async def margin_by_counterparty(session: AsyncSession) -> dict:
    rows = await _grouped(session, "counterparty_ref")
    return {"currency": "BYN", "items": rows}

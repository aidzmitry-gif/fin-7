"""Центры затрат: справочник-константа + группировка платежей за период.

Справочник центров — захардкоженный список (не отдельная таблица — YAGNI; апгрейд в
таблицу справочника — # ponytail, когда появится финансовый аналитик и захочет вводить
свои центры). Маппинг ``kind → center`` — дефолт; при наличии явного ``cost_center`` в
платеже используется он.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment

# ponytail: при росте — таблица finance.cost_center и роли «владельца центра» в RBAC.
DEFAULT_BY_KIND = {
    "landed": "Закупки",
    "freight": "Логистика",
    "freight_refund": "Логистика",
    "receivable": "Продажи",
}
KNOWN_CENTERS = ("Закупки", "Логистика", "Продажи")


def resolve_center(p: Payment) -> str:
    explicit = getattr(p, "cost_center", None)
    if explicit:
        return explicit
    return DEFAULT_BY_KIND.get(p.kind, "Прочее")


async def group_by_cost_center(
    session: AsyncSession,
    from_: date | None = None,
    to: date | None = None,
) -> dict:
    """Сумма по центрам затрат за период. Расход/доход разводим по знаку kind."""
    q = select(Payment)
    if from_ is not None:
        q = q.where(Payment.created_at >= from_)
    if to is not None:
        q = q.where(Payment.created_at < to)
    rows = (await session.execute(q)).scalars().all()
    by: dict[str, dict[str, Decimal]] = {}
    for p in rows:
        center = resolve_center(p)
        slot = by.setdefault(center, {"income": Decimal("0"), "expense": Decimal("0")})
        amt = Decimal(str(p.amount))
        if p.kind == "receivable":
            slot["income"] += amt
        elif p.kind == "freight_refund":
            # возврат фрахта — отрицательная сумма → уменьшает «expense» через += знаков
            slot["expense"] += amt
        else:  # freight / landed / прочее → расход
            slot["expense"] += amt
    return {
        "from": from_.isoformat() if from_ else None,
        "to": to.isoformat() if to else None,
        "currency": "BYN",
        "centers": [
            {"name": name, "income": float(by[name]["income"]), "expense": float(by[name]["expense"])}
            for name in sorted(by, key=lambda n: -float(by[n]["expense"]))
        ],
    }

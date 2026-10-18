"""ДДС (Движение Денежных Средств) — cash-basis отчёт.

P&L = accrual (по начислению). Эта проекция ДДС использует статусы платежей и не подтверждает банковское движение.

Поступления (inflows):
  Payment WHERE status IN ('paid', 'received') AND kind = 'receivable'

Выбытия (outflows):
  Payment WHERE status = 'paid' AND kind IN ('payroll', 'opex', 'tax', 'bank_fee',
                                              'freight', 'landed', 'po_planned')

Нетто = inflows − outflows.
Остаток банка неизвестен без подтверждённого источника выбранной организации.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment

_INFLOW_KIND = ("receivable",)
_INFLOW_STATUS = ("paid", "received")

_OUTFLOW_STATUS = ("paid",)
_OUTFLOW_KINDS = ("payroll", "opex", "tax", "bank_fee", "freight", "landed", "po_planned")

_ALL_BREAKDOWN_KINDS = ("receivable",) + _OUTFLOW_KINDS


def _fmt(d: Decimal) -> str:
    return f"{d:.2f}"


async def cashflow_report(
    session: AsyncSession,
    from_date: date | None = None,
    to_date: date | None = None,
    *,
    organization_id: int,
) -> dict:
    """ДДС-отчёт за период (все суммы — строки BYN).

    Параметры:
        from_date / to_date — фильтр по created_at (оба опциональны).
    """
    if type(organization_id) is not int or organization_id <= 0:
        raise ValueError("A positive explicit organization_id is required")
    # ──── inflows: receivable paid/received ────
    q_in = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.organization_id == organization_id,
        Payment.kind.in_(_INFLOW_KIND),
        Payment.status.in_(_INFLOW_STATUS),
    )
    if from_date is not None:
        q_in = q_in.where(func.date(Payment.created_at) >= from_date.isoformat())
    if to_date is not None:
        q_in = q_in.where(func.date(Payment.created_at) <= to_date.isoformat())

    inflows = Decimal(str((await session.execute(q_in)).scalar_one()))

    # ──── outflows: каждый kind отдельно ────
    q_out = (
        select(Payment.kind, func.coalesce(func.sum(Payment.amount), 0))
        .where(
            Payment.organization_id == organization_id,
            Payment.status.in_(_OUTFLOW_STATUS),
            Payment.kind.in_(_OUTFLOW_KINDS),
        )
        .group_by(Payment.kind)
    )
    if from_date is not None:
        q_out = q_out.where(func.date(Payment.created_at) >= from_date.isoformat())
    if to_date is not None:
        q_out = q_out.where(func.date(Payment.created_at) <= to_date.isoformat())

    out_by_kind: dict[str, Decimal] = {k: Decimal("0") for k in _OUTFLOW_KINDS}
    for kind, total in (await session.execute(q_out)).all():
        if kind in out_by_kind:
            out_by_kind[kind] = Decimal(str(total))

    outflows = sum(out_by_kind.values(), Decimal("0"))
    net = inflows - outflows

    return {
        "calculation_basis": "Полные суммы по статусам платежей и дате создания; частичные оплаты не включены.",
        "limitations": [
            "Это проекция зарегистрированных статусов, не подтверждение банковского движения.",
            "Банковский остаток выбранной организации не подтверждён.",
        ],
        "period_from": from_date.isoformat() if from_date else None,
        "period_to": to_date.isoformat() if to_date else None,
        "currency": "BYN",
        "inflows": _fmt(inflows),
        "outflows": _fmt(outflows),
        "net_cashflow": _fmt(net),
        "bank_balance": None,
        "breakdown": {
            "receivable": _fmt(inflows),
            **{k: _fmt(out_by_kind[k]) for k in _OUTFLOW_KINDS},
        },
    }

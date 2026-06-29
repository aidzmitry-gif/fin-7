"""P&L (отчёт о прибылях и убытках) — операционный контур, accrual-basis.

Все суммы — строки BYN (Decimal, точность до копейки). Знаки в аргументах:
  - revenue_recognized: + (признанная выручка по отгрузке, accrual)
  - landed: +  (себестоимость прихода)
  - claim_refund: +  (компенсация поставщика, уменьшает COGS)
  - freight: +  (расход на перевозку)
  - freight_refund: − в БД (хранится отрицательной, уменьшает freight)
  - payroll, opex, tax, bank_fee: + (операционные расходы)

НЕ смешивать с summary.py (cash-basis: receivable) — разные оси учёта.
Ledger/НДС/ЭСЧФ остаются в 1С.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment

_REVENUE_KIND = "revenue_recognized"
_COGS_KIND = "landed"
_CLAIM_KIND = "claim_refund"  # уменьшает COGS
_FREIGHT_KIND = "freight"
_REFUND_KIND = "freight_refund"  # хранится отрицательной
_PAYROLL_KIND = "payroll"
_OPEX_KIND = "opex"
_TAX_KIND = "tax"
_BANK_KIND = "bank_fee"

_ALL_PNL_KINDS = (
    _REVENUE_KIND,
    _COGS_KIND,
    _CLAIM_KIND,
    _FREIGHT_KIND,
    _REFUND_KIND,
    _PAYROLL_KIND,
    _OPEX_KIND,
    _TAX_KIND,
    _BANK_KIND,
)


def _fmt(d: Decimal) -> str:
    """Форматировать Decimal как строку с 2 знаками (BYN-копейки, без float-дрейфа)."""
    return f"{d:.2f}"


async def pnl_report(
    session: AsyncSession,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """Агрегат P&L за период (все суммы — строки BYN).

    Формула:
        gross_profit = revenue - cogs_net
        cogs_net     = landed - claim_refund
        freight_net  = freight + freight_refund  (freight_refund < 0 в БД)
        operating_profit = gross_profit - freight_net - payroll - opex - tax - bank_fee

    Все компоненты возвращаются строками для избежания float-дрейфа.
    """
    q_base = select(Payment.kind, func.coalesce(func.sum(Payment.amount), 0)).group_by(
        Payment.kind
    )
    if from_date is not None:
        q_base = q_base.where(Payment.created_at >= from_date)
    if to_date is not None:
        q_base = q_base.where(Payment.created_at <= to_date)

    by_kind: dict[str, Decimal] = {k: Decimal("0") for k in _ALL_PNL_KINDS}
    for kind, total in (await session.execute(q_base)).all():
        if kind in by_kind:
            by_kind[kind] = Decimal(str(total))

    revenue = by_kind[_REVENUE_KIND]
    landed = by_kind[_COGS_KIND]
    claim_refund = by_kind[_CLAIM_KIND]
    freight = by_kind[_FREIGHT_KIND]
    freight_refund = by_kind[_REFUND_KIND]  # отрицательная
    payroll = by_kind[_PAYROLL_KIND]
    opex = by_kind[_OPEX_KIND]
    tax = by_kind[_TAX_KIND]
    bank_fee = by_kind[_BANK_KIND]

    cogs_net = landed - claim_refund
    freight_net = freight + freight_refund  # freight_refund < 0 → уменьшает
    gross_profit = revenue - cogs_net
    operating_profit = gross_profit - freight_net - payroll - opex - tax - bank_fee

    return {
        "from_date": from_date.isoformat() if from_date else None,
        "to_date": to_date.isoformat() if to_date else None,
        "currency": "BYN",
        "revenue": _fmt(revenue),
        "cogs_gross": _fmt(landed),
        "cogs_claim_refund": _fmt(claim_refund),
        "cogs_net": _fmt(cogs_net),
        # alias для краткого UI и тестов (cogs = cogs_net, freight = freight_net)
        "cogs": _fmt(cogs_net),
        "freight_gross": _fmt(freight),
        "freight_refund": _fmt(freight_refund),
        "freight_net": _fmt(freight_net),
        "freight": _fmt(freight_net),
        "gross_profit": _fmt(gross_profit),
        "payroll": _fmt(payroll),
        "opex": _fmt(opex),
        "tax": _fmt(tax),
        "bank_fee": _fmt(bank_fee),
        "operating_profit": _fmt(operating_profit),
    }

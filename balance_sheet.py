"""Предварительный управленческий обзор; не регламентированный баланс.
Капитал без бухгалтерских остатков не вычисляется из разницы итогов.
Недоступные компоненты активов сохраняются неизвестными, а не нулевыми.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment

_AR_KIND = ("receivable",)
_AR_STATUS = ("recognized", "pending")

_AP_KINDS = ("po_planned", "freight", "landed")
_PAYROLL_KIND = "payroll"
_TAX_KIND = "tax"
_PENDING = "pending"


def _fmt(d: Decimal) -> str:
    if not d.is_finite():
        raise ValueError("Financial report amount must be finite")
    return f"{d:.2f}"


async def get_balance_sheet(
    session: AsyncSession,
    as_of_date: date,
    services: Any = None,
) -> dict:
    """Управленческий обзор на дату; неподтверждённые значения остаются None.

    cash / inventory_value — str | None (None при mock / недоступном шлюзе).
    """
    # func.date() приводит datetime → date для совместимости SQLite/Postgres
    as_of_iso = as_of_date.isoformat()

    # ──── Дебиторка: receivable pending/recognized ────
    q_ar = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.kind.in_(_AR_KIND),
        Payment.status.in_(_AR_STATUS),
        func.date(Payment.created_at) <= as_of_iso,
    )
    accounts_receivable = Decimal(str((await session.execute(q_ar)).scalar_one()))

    # ──── Кредиторка: po_planned/freight/landed pending ────
    q_ap = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.kind.in_(_AP_KINDS),
        Payment.status == _PENDING,
        func.date(Payment.created_at) <= as_of_iso,
    )
    accounts_payable = Decimal(str((await session.execute(q_ap)).scalar_one()))

    # ──── Долг по зарплате ────
    q_pay = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.kind == _PAYROLL_KIND,
        Payment.status == _PENDING,
        func.date(Payment.created_at) <= as_of_iso,
    )
    payroll_payable = Decimal(str((await session.execute(q_pay)).scalar_one()))

    # ──── Долг по налогам ────
    q_tax = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.kind == _TAX_KIND,
        Payment.status == _PENDING,
        func.date(Payment.created_at) <= as_of_iso,
    )
    tax_payable = Decimal(str((await session.execute(q_tax)).scalar_one()))

    # ──── Банковский остаток из 1С (honest-empty) ────
    cash: str | None = None
    onec = getattr(services, "onec", None) if services is not None else None
    if onec is not None:
        try:
            raw = await onec.fetch_bank_balance()
            if raw is not None:
                # fetch_bank_balance может вернуть dict или str/число
                val = raw.get("balance", raw) if isinstance(raw, dict) else raw
                cash = _fmt(Decimal(str(val)))
        except Exception:  # noqa: BLE001 — fail-soft
            pass

    # ──── Остаток товаров из 1С (honest-empty) ────
    inventory_value: str | None = None
    if onec is not None:
        try:
            bs = await onec.fetch_balance_sheet(as_of_date)
            if bs is not None and bs.get("inventory") is not None:
                inventory_value = _fmt(Decimal(str(bs["inventory"])))
        except Exception:  # noqa: BLE001 — fail-soft
            pass

    # No synthetic equity or complete asset total from unknown components.
    total_assets = None
    if cash is not None and inventory_value is not None:
        total_assets = accounts_receivable + Decimal(cash) + Decimal(inventory_value)
    total_liabilities = accounts_payable + payroll_payable + tax_payable
    limitations = [
        "Капитал не подтверждён бухгалтерскими остатками.",
        "Расчёты используют текущие статусы и не подтверждают исторический баланс.",
        "Отчёт не разделяет данные по юридическим лицам.",
    ]
    if cash is None:
        limitations.append("Банковский остаток не получен.")
    else:
        limitations.append("Банковский остаток не подтверждён на выбранную дату.")
    if inventory_value is None:
        limitations.append("Стоимость запасов не получена.")
    return {
        "as_of": as_of_date.isoformat(),
        "currency": "BYN",
        # Активы
        "accounts_receivable": _fmt(accounts_receivable),
        "cash": cash,
        "inventory_value": inventory_value,
        "total_assets": _fmt(total_assets) if total_assets is not None else None,
        # Пассивы
        "accounts_payable": _fmt(accounts_payable),
        "payroll_payable": _fmt(payroll_payable),
        "tax_payable": _fmt(tax_payable),
        "total_liabilities": _fmt(total_liabilities),
        # Капитал
        "equity": None,
        "report_kind": "management_snapshot",
        "preliminary": True,
        "statutory_certified": False,
        "limitations": limitations,
    }

"""Предварительный управленческий обзор; не регламентированный баланс.
Капитал без бухгалтерских остатков не вычисляется из разницы итогов.
Недоступные компоненты активов сохраняются неизвестными, а не нулевыми.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

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
    *,
    organization_id: int,
) -> dict:
    """Управленческий обзор на дату; неподтверждённые значения остаются None.

    cash / inventory_value — str | None (None при mock / недоступном шлюзе).
    """
    if type(organization_id) is not int or organization_id <= 0:
        raise ValueError("A positive explicit organization_id is required")
    # func.date() приводит datetime → date для совместимости SQLite/Postgres
    as_of_iso = as_of_date.isoformat()

    # ──── Дебиторка: receivable pending/recognized ────
    q_ar = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.organization_id == organization_id,
        Payment.kind.in_(_AR_KIND),
        Payment.status.in_(_AR_STATUS),
        func.date(Payment.created_at) <= as_of_iso,
    )
    accounts_receivable = Decimal(str((await session.execute(q_ar)).scalar_one()))

    # ──── Кредиторка: po_planned/freight/landed pending ────
    q_ap = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.organization_id == organization_id,
        Payment.kind.in_(_AP_KINDS),
        Payment.status == _PENDING,
        func.date(Payment.created_at) <= as_of_iso,
    )
    accounts_payable = Decimal(str((await session.execute(q_ap)).scalar_one()))

    # ──── Долг по зарплате ────
    q_pay = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.organization_id == organization_id,
        Payment.kind == _PAYROLL_KIND,
        Payment.status == _PENDING,
        func.date(Payment.created_at) <= as_of_iso,
    )
    payroll_payable = Decimal(str((await session.execute(q_pay)).scalar_one()))

    # ──── Долг по налогам ────
    q_tax = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.organization_id == organization_id,
        Payment.kind == _TAX_KIND,
        Payment.status == _PENDING,
        func.date(Payment.created_at) <= as_of_iso,
    )
    tax_payable = Decimal(str((await session.execute(q_tax)).scalar_one()))

    # External assets require a trusted organization-specific source.
    total_liabilities = accounts_payable + payroll_payable + tax_payable
    limitations = [
        "Капитал не подтверждён бухгалтерскими остатками.",
        "Расчёты используют текущие статусы и не подтверждают исторический баланс.",
        "Полнота обязательств выбранной организации не подтверждена.",
        "Частичные оплаты и их даты не реконструируют историческую задолженность.",
        "Банковский остаток выбранной организации не подтверждён.",
        "Стоимость запасов выбранной организации не подтверждена.",
    ]
    return {
        "calculation_basis": "Полные суммы по текущим статусам и дате создания; частичные оплаты не вычтены.",
        "as_of": as_of_date.isoformat(),
        "currency": "BYN",
        # Активы
        "accounts_receivable": _fmt(accounts_receivable),
        "cash": None,
        "inventory_value": None,
        "total_assets": None,
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

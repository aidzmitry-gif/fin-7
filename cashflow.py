"""Cash-flow прогноз: понедельная/подневная проекция по due_date неоплаченных платежей.

Приток = ``receivable`` с outstanding > 0 по неделям/дням ``due_date``.
Отток = ``freight``/``landed``/``po_planned`` с outstanding > 0 по неделям/дням ``due_date``.
Платежи без ``due_date`` — отдельная корзина ``not_dated`` (не размазываем).

Р4: добавлен ``mode='day'`` (день вместо недели) + опц. фильтр ``account_id`` (бакетируем
только платежи на этом счёте; для управленческого расчёта остатка по конкретному счёту).
``opening_balance`` — оплаченные счета − оплаченные расходы (по выбранному счёту, если задан).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.aging import _payments_outstanding
from modules.finance.schemas import money_str


class FinanceAccountUnavailable(ValueError):
    """The selected account is unavailable in the requested organization."""


Mode = Literal["week", "day"]


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def _opening_balance(
    session: AsyncSession, account_id: int | None = None, *, organization_id: int
) -> Decimal:
    """Текущее сальдо: фактически оплаченные счета − фактически оплаченные расходы.

    Если задан ``account_id`` — считаем по конкретному счёту (платежи без account_id не
    суммируются — это намеренно: «общий кэш» не привязан ни к одному счёту, не утаскиваем
    его сальдо в отдельный счёт).
    """
    from modules.finance.models import BankAccount, Payment

    if type(organization_id) is not int or organization_id <= 0:
        raise ValueError("organization_id must be a positive integer")
    if account_id is not None and (type(account_id) is not int or account_id <= 0):
        raise ValueError("account_id must be a positive integer")
    acc = None
    if account_id is not None:
        acc = (await session.execute(
            select(BankAccount).where(
                BankAccount.id == account_id, BankAccount.organization_id == organization_id
            )
        )).scalar_one_or_none()
        if acc is None:
            raise FinanceAccountUnavailable("Bank account is unavailable for this organization")

    def _scoped(q):
        q = q.where(Payment.organization_id == organization_id)
        if account_id is not None:
            q = q.where(Payment.account_id == account_id)
        return q

    received = (
        await session.execute(
            _scoped(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.kind == "receivable", Payment.status == "paid"
                )
            )
        )
    ).scalar_one()
    paid_out = (
        await session.execute(
            _scoped(
                select(func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.kind.in_(("freight", "landed")), Payment.status == "paid"
                )
            )
        )
    ).scalar_one()
    cashflow_opening = Decimal(str(received)) - Decimal(str(paid_out))
    # начальное сальдо BankAccount (если есть и совпадает аккаунт) — прибавляем
    if acc is not None:
        opening = Decimal(str(acc.opening_balance))
        if opening and (acc.currency or "BYN").strip().upper() != "BYN":
            from core.services.nbrb import RateUnavailable, convert

            if acc.opening_at is None:
                raise RateUnavailable("Укажите дату начального валютного остатка")
            opening, _ = await convert(session, opening, acc.currency, acc.opening_at)
        cashflow_opening += opening
    return cashflow_opening


async def cashflow_forecast(
    session: AsyncSession,
    weeks: int = 8,
    today: date | None = None,
    mode: Mode = "week",
    days: int = 30,
    account_id: int | None = None,
    *,
    organization_id: int,
) -> dict:
    """Понедельный (mode='week') или подневный (mode='day') прогноз.

    week: ``weeks`` корзин по 7 дней; первая корзина — понедельник текущей недели.
    day:  ``days`` корзин по 1 дню; первая корзина — сегодня.
    ``account_id`` (опц.) — бакетируем только платежи этого счёта.
    """
    opening = await _opening_balance(session, account_id, organization_id=organization_id)
    today = today or date.today()
    if mode == "day":
        bucket_count = max(1, min(days, 90))
        bucket_starts = [today + timedelta(days=i) for i in range(bucket_count)]
        bucket_size = timedelta(days=1)
        def _key(d: date) -> date:
            return d
    else:  # week
        bucket_count = max(1, min(weeks, 52))
        base = _monday(today)
        bucket_starts = [base + timedelta(weeks=i) for i in range(bucket_count)]
        bucket_size = timedelta(weeks=1)
        def _key(d: date) -> date:
            return _monday(d)

    buckets_map: dict[date, dict[str, Decimal]] = {
        w: {"inflow": Decimal("0"), "outflow": Decimal("0")} for w in bucket_starts
    }
    not_dated_in = Decimal("0")
    not_dated_out = Decimal("0")

    def _belongs(p) -> bool:
        return account_id is None or p.account_id == account_id

    for p, outstanding in await _payments_outstanding(session, ("receivable",), organization_id=organization_id):
        if not _belongs(p):
            continue
        if p.due_date is None:
            not_dated_in += outstanding
            continue
        k = _key(p.due_date)
        if k in buckets_map:
            buckets_map[k]["inflow"] += outstanding

    # отток включает PO_planned (FIN-B1) — это ещё план, но для прогноза кассы важен
    for p, outstanding in await _payments_outstanding(
        session, ("freight", "landed", "po_planned"), organization_id=organization_id
    ):
        if not _belongs(p):
            continue
        if p.due_date is None:
            not_dated_out += outstanding
            continue
        k = _key(p.due_date)
        if k in buckets_map:
            buckets_map[k]["outflow"] += outstanding

    rows = []
    cumulative = opening
    for w in bucket_starts:
        inflow = buckets_map[w]["inflow"]
        outflow = buckets_map[w]["outflow"]
        net = inflow - outflow
        cumulative += net
        rows.append(
            {
                "bucket_start": w.isoformat(),
                "inflow": money_str(inflow),
                "outflow": money_str(outflow),
                "net": money_str(net),
                "cumulative": money_str(cumulative),
            }
        )
    return {
        "as_of": today.isoformat(),
        "currency": "BYN",
        "mode": mode,
        "bucket_size_days": bucket_size.days,
        "account_id": account_id,
        "opening_balance": money_str(opening),
        "buckets": rows,
        # ponytail-совместимость: оставляем поле `weeks` как алиас на buckets, чтобы
        # не сломать прошлый фронт (он смотрит на week_start). Поле для week-mode.
        "weeks": (
            [{**r, "week_start": r["bucket_start"]} for r in rows] if mode == "week" else []
        ),
        "not_dated": {"inflow": money_str(not_dated_in), "outflow": money_str(not_dated_out)},
    }

"""Cash-flow прогноз: понедельная проекция по due_date неоплаченных платежей.

Приток = ``receivable`` с outstanding > 0 по неделям ``due_date``.
Отток = ``freight``/``landed`` с outstanding > 0 по неделям ``due_date``.
Платежи без ``due_date`` — отдельная корзина ``not_dated`` (не размазываем по неделям).
``opening_balance`` = текущее сальдо кассы = оплаченные счета − оплаченные расходы.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.aging import _payments_outstanding


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def _opening_balance(session: AsyncSession) -> Decimal:
    """Текущее сальдо: фактически оплаченные счета − фактически оплаченные расходы."""
    from modules.finance.models import Payment

    received = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.kind == "receivable", Payment.status == "paid"
            )
        )
    ).scalar_one()
    paid_out = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.kind.in_(("freight", "landed")), Payment.status == "paid"
            )
        )
    ).scalar_one()
    return Decimal(str(received)) - Decimal(str(paid_out))


async def cashflow_forecast(
    session: AsyncSession, weeks: int = 8, today: date | None = None
) -> dict:
    today = today or date.today()
    base = _monday(today)
    week_starts = [base + timedelta(weeks=i) for i in range(weeks)]
    weeks_map: dict[date, dict[str, Decimal]] = {
        w: {"inflow": Decimal("0"), "outflow": Decimal("0")} for w in week_starts
    }
    not_dated_in = Decimal("0")
    not_dated_out = Decimal("0")

    for p, outstanding in await _payments_outstanding(session, ("receivable",)):
        if p.due_date is None:
            not_dated_in += outstanding
            continue
        w = _monday(p.due_date)
        if w in weeks_map:
            weeks_map[w]["inflow"] += outstanding

    for p, outstanding in await _payments_outstanding(session, ("freight", "landed")):
        if p.due_date is None:
            not_dated_out += outstanding
            continue
        w = _monday(p.due_date)
        if w in weeks_map:
            weeks_map[w]["outflow"] += outstanding

    opening = await _opening_balance(session)
    rows = []
    cumulative = opening
    for w in week_starts:
        inflow = weeks_map[w]["inflow"]
        outflow = weeks_map[w]["outflow"]
        net = inflow - outflow
        cumulative += net
        rows.append(
            {
                "week_start": w.isoformat(),
                "inflow": float(inflow),
                "outflow": float(outflow),
                "net": float(net),
                "cumulative": float(cumulative),
            }
        )
    return {
        "as_of": today.isoformat(),
        "currency": "BYN",
        "opening_balance": float(opening),
        "weeks": rows,
        "not_dated": {"inflow": float(not_dated_in), "outflow": float(not_dated_out)},
    }

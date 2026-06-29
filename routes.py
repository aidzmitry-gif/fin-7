"""HTTP-API модуля Finance. Монтируется под префиксом ``/finance``."""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from modules.finance.models import BankAccount, Payment, PaymentAllocation
from modules.finance.schemas import (
    AllocationCreate,
    AllocationOut,
    BankAccountCreate,
    BankAccountOut,
    BankAccountUpdate,
    PaymentCreate,
    PaymentDetail,
    PaymentOut,
    StatusUpdate,
)
from modules.finance.summary import finance_summary

router = APIRouter(tags=["finance"])


# ───────────────────────── Сводка / Aging / Cash-flow / Маржа / Сверка ─────────────────────────


@router.get("/summary")
async def get_summary(session: AsyncSession = Depends(get_session)):
    """Операционная сводка: фактическая маржа (выручка − landed − фрахт) + касса (ДДС-lite)."""
    return await finance_summary(session)


@router.get("/aging")
async def get_aging(session: AsyncSession = Depends(get_session)):
    """AR/AP по корзинам срока (current/1-30/31-60/61-90/90+/без срока)."""
    from modules.finance.aging import aging_buckets

    return await aging_buckets(session)


@router.get("/cashflow-forecast")
async def get_cashflow_forecast(
    weeks: int = 8,
    mode: str = "week",
    days: int = 30,
    account_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Понедельный (mode='week') или подневный (mode='day') прогноз кэш-фло (Р4).

    Опц. ``account_id`` — фильтр по банк-счёту (платежи без account_id — общий кэш,
    не суммируются в выбранный счёт). Без ``account_id`` — все платежи.
    """
    from modules.finance.cashflow import cashflow_forecast

    if mode not in ("week", "day"):
        mode = "week"
    return await cashflow_forecast(
        session,
        weeks=max(1, min(weeks, 52)),
        days=max(1, min(days, 90)),
        mode=mode,  # type: ignore[arg-type]
        account_id=account_id,
    )


@router.get("/by-cost-center")
async def get_by_cost_center(
    session: AsyncSession = Depends(get_session),
    from_: str | None = None,
    to: str | None = None,
):
    """Суммы расходов/доходов по центрам затрат за период (даты ISO)."""
    from modules.finance.cost_center import group_by_cost_center

    return await group_by_cost_center(session, _safe_date(from_), _safe_date(to))


@router.get("/margin/by-deal")
async def margin_by_deal(session: AsyncSession = Depends(get_session)):
    """Маржа по сделкам: выручка − landed − чистый фрахт, сорт. по убыванию валовой."""
    from modules.finance.margin import margin_by_deal as fn

    return await fn(session)


@router.get("/margin/by-counterparty")
async def margin_by_counterparty(session: AsyncSession = Depends(get_session)):
    """Маржа по контрагентам (provенанс MDM); группа «не атрибутировано» отдельно."""
    from modules.finance.margin import margin_by_counterparty as fn

    return await fn(session)


@router.get("/margin/reconcile-deal")
async def margin_reconcile_deal(
    deal_id: int,
    items: str | None = None,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сходимость landed по сделке: finance (проводки) ↔ facade (landed-cost ядра).

    Параметры:
        - ``deal_id`` — сделка для сверки.
        - ``items`` — опц. ``SKU1:qty1,SKU2:qty2`` — список SKU+qty позиций сделки для
          расчёта контрольной величины через ``core.services.landed_cost``. Без ``items``
          или с отсутствующим фасадом → ``source_facade_available=false`` (honest-empty).
    """
    from modules.finance.margin import _parse_items, reconcile_deal_margin

    facade = getattr(core.services, "landed_cost", None)
    return await reconcile_deal_margin(session, facade, deal_id, _parse_items(items))


@router.get("/reconcile-1c")
async def reconcile_1c(
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сверка платежей ERP с 1С (ЧТЕНИЕ, fail-soft). 1С недоступна → source_available=False."""
    from modules.finance.reconcile import reconcile_with_onec

    return await reconcile_with_onec(session, getattr(core.services, "onec", None))


# ───────────────────────── P&L (Р5) ─────────────────────────


@router.get("/pnl")
async def get_pnl(
    period_from: str | None = None,
    period_to: str | None = None,
    format: str | None = None,  # noqa: A002 — shadowing built-in ok для query-param
    fmt: str | None = None,  # обратная совместимость: ?fmt=csv
    session: AsyncSession = Depends(get_session),
):
    """P&L за период (accrual-basis). Все суммы — строки BYN (Decimal, без float-дрейфа).

    ``period_from`` / ``period_to`` — ISO-даты; некорректный формат → 400.
    ``?format=csv`` (или ``?fmt=csv``) — CSV-выгрузка для скачивания.

    Источник выручки: ``kind=revenue_recognized`` (accrual по отгрузке, ``sales.deal.handoff``),
    НЕ ``receivable`` (кассовые счета). Summary/aging по-прежнему работают с ``receivable``.
    """
    from modules.finance.pnl import pnl_report

    # Валидация дат: некорректная ISO → 400
    from_dt = _safe_date(period_from)
    to_dt = _safe_date(period_to)
    if period_from and from_dt is None:
        raise HTTPException(status_code=400, detail=f"Некорректная дата period_from: {period_from!r}")
    if period_to and to_dt is None:
        raise HTTPException(status_code=400, detail=f"Некорректная дата period_to: {period_to!r}")

    data = await pnl_report(session, from_dt, to_dt)

    csv_requested = (format or fmt) == "csv"
    if csv_requested:
        period_label = f"{period_from or 'начало'} — {period_to or 'сейчас'}"
        lines = [f"Показатель,Сумма BYN,Период {period_label}"]
        for label, key in [
            ("Выручка (признанная)", "revenue"),
            ("Себестоимость (landed)", "cogs_gross"),
            ("Компенсации по претензиям", "cogs_claim_refund"),
            ("Себестоимость (нетто)", "cogs_net"),
            ("Валовая прибыль", "gross_profit"),
            ("Фрахт (брутто)", "freight_gross"),
            ("Возврат фрахта", "freight_refund"),
            ("Фрахт (нетто)", "freight_net"),
            ("ФОТ", "payroll"),
            ("Прочие операционные (opex)", "opex"),
            ("Налоги", "tax"),
            ("Банковские расходы", "bank_fee"),
            ("Операционная прибыль", "operating_profit"),
        ]:
            lines.append(f"{label},{data[key]}")
        csv_body = "\n".join(lines) + "\n"
        fn = f"pnl_{period_from or 'all'}_{period_to or 'now'}.csv"
        return Response(
            content=csv_body,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={fn}"},
        )

    return data


def _safe_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


# ───────────────────────── Банковские счета (Р4) ─────────────────────────


@router.get("/bank-accounts", response_model=list[BankAccountOut])
async def list_bank_accounts(
    active_only: bool = True, session: AsyncSession = Depends(get_session)
):
    """Список банковских счетов (по умолчанию только активные)."""
    q = select(BankAccount).order_by(BankAccount.id)
    if active_only:
        q = q.where(BankAccount.is_active == 1)
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": a.id,
            "code": a.code,
            "title": a.title,
            "currency": a.currency,
            "opening_balance": float(a.opening_balance),
            "opening_at": a.opening_at,
            "is_active": bool(a.is_active),
        }
        for a in rows
    ]


@router.post("/bank-accounts", response_model=BankAccountOut, status_code=201)
async def create_bank_account(
    payload: BankAccountCreate, session: AsyncSession = Depends(get_session)
):
    """Завести банк-счёт / кассу. ``code`` уникален."""
    exists = (
        await session.execute(select(BankAccount).where(BankAccount.code == payload.code))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail=f"Счёт {payload.code!r} уже существует")
    obj = BankAccount(
        code=payload.code,
        title=payload.title,
        currency=payload.currency,
        opening_balance=Decimal(str(payload.opening_balance)),
        opening_at=payload.opening_at,
        is_active=1,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return {
        "id": obj.id,
        "code": obj.code,
        "title": obj.title,
        "currency": obj.currency,
        "opening_balance": float(obj.opening_balance),
        "opening_at": obj.opening_at,
        "is_active": bool(obj.is_active),
    }


@router.patch("/bank-accounts/{account_id}", response_model=BankAccountOut)
async def update_bank_account(
    account_id: int,
    payload: BankAccountUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Частичное обновление: title / opening / активность. ``code`` менять нельзя."""
    obj = await session.get(BankAccount, account_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    data = payload.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        obj.title = data["title"]
    if "is_active" in data and data["is_active"] is not None:
        obj.is_active = 1 if data["is_active"] else 0
    if "opening_balance" in data and data["opening_balance"] is not None:
        obj.opening_balance = Decimal(str(data["opening_balance"]))
    if "opening_at" in data:
        obj.opening_at = data["opening_at"]
    await session.commit()
    await session.refresh(obj)
    return {
        "id": obj.id,
        "code": obj.code,
        "title": obj.title,
        "currency": obj.currency,
        "opening_balance": float(obj.opening_balance),
        "opening_at": obj.opening_at,
        "is_active": bool(obj.is_active),
    }


# ───────────────────────── Платежи ─────────────────────────


async def _sum_allocations(session: AsyncSession, payment_id: int) -> Decimal:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(
                PaymentAllocation.payment_id == payment_id
            )
        )
    ).scalar_one()
    return Decimal(str(total))


def _enrich(p: Payment, allocated: Decimal, today: date | None = None) -> dict:
    """ORM Payment → словарь для PaymentOut с вычисляемыми outstanding/is_overdue."""
    today = today or date.today()
    outstanding = Decimal(str(p.amount)) - allocated
    is_overdue = (
        p.status in ("pending", "partial")
        and p.due_date is not None
        and p.due_date < today
    )
    return {
        "id": p.id,
        "ref": p.ref,
        "amount": float(p.amount),
        "status": p.status,
        "kind": p.kind,
        "due_date": p.due_date,
        "paid_at": p.paid_at,
        "deal_id": p.deal_id,
        "counterparty_ref": p.counterparty_ref,
        "account_id": p.account_id,
        "outstanding": float(outstanding),
        "is_overdue": is_overdue,
    }


@router.get("/payments", response_model=list[PaymentOut])
async def list_payments(
    account_id: int | None = None, session: AsyncSession = Depends(get_session)
):
    """Платежи (с outstanding и is_overdue, посчитанными на чтении). Опц. фильтр по счёту."""
    q = select(Payment).order_by(Payment.id.desc())
    if account_id is not None:
        q = q.where(Payment.account_id == account_id)
    rows = (await session.execute(q)).scalars().all()
    # Aggregated allocations: один запрос вместо N+1
    sums = dict(
        (
            await session.execute(
                select(PaymentAllocation.payment_id, func.sum(PaymentAllocation.amount)).group_by(
                    PaymentAllocation.payment_id
                )
            )
        ).all()
    )
    today = date.today()
    return [_enrich(p, Decimal(str(sums.get(p.id, 0))), today) for p in rows]


@router.get("/payments/{payment_id}", response_model=PaymentDetail)
async def get_payment(payment_id: int, session: AsyncSession = Depends(get_session)):
    """Платёж + список частичных поступлений + остаток (outstanding)."""
    obj = await session.get(Payment, payment_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    allocs = (
        await session.execute(
            select(PaymentAllocation)
            .where(PaymentAllocation.payment_id == payment_id)
            .order_by(PaymentAllocation.id.desc())
        )
    ).scalars().all()
    allocated = sum((Decimal(str(a.amount)) for a in allocs), Decimal("0"))
    out = _enrich(obj, allocated)
    out["allocations"] = [AllocationOut.model_validate(a).model_dump() for a in allocs]
    return out


@router.post("/payments", response_model=PaymentOut, status_code=201)
async def create_payment(payload: PaymentCreate, session: AsyncSession = Depends(get_session)):
    """Зафиксировать платёж (lifecycle + провенанс — опционально)."""
    obj = Payment(
        ref=payload.ref,
        amount=Decimal(str(payload.amount)),
        status=payload.status,
        kind=payload.kind,
        due_date=payload.due_date,
        deal_id=payload.deal_id,
        counterparty_ref=payload.counterparty_ref,
        account_id=payload.account_id,
    )
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return _enrich(obj, Decimal("0"))


@router.patch("/payments/{payment_id}", response_model=PaymentOut)
async def update_payment(
    payment_id: int,
    payload: StatusUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Сменить статус платежа. При ``paid`` — paid_at=now() + событие finance.payment.paid."""
    obj = await session.get(Payment, payment_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    obj.status = payload.status
    if payload.status == "paid":
        obj.paid_at = datetime.now(UTC)
        core.event_bus.emit(
            session, "finance.payment.paid", {"ref": obj.ref, "entity_ref": f"payment:{obj.id}"}
        )
    await session.commit()
    await session.refresh(obj)
    allocated = await _sum_allocations(session, payment_id)
    return _enrich(obj, allocated)


@router.post(
    "/payments/{payment_id}/allocations",
    response_model=AllocationOut,
    status_code=201,
)
async def create_allocation(
    payment_id: int,
    payload: AllocationCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
):
    """Зафиксировать частичное поступление. Статус платежа авто:
    sum=amount → paid (эмит finance.payment.paid), 0<sum<amount → partial, =0 → pending.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Платёж не найден")
    amt = Decimal(str(payload.amount))
    if amt <= 0:
        raise HTTPException(status_code=400, detail="Сумма поступления должна быть > 0")
    alloc = PaymentAllocation(payment_id=payment_id, amount=amt)
    session.add(alloc)
    await session.flush()
    total = await _sum_allocations(session, payment_id)
    target = Decimal(str(payment.amount))
    outstanding_after = target - total
    # FIN-C3: эмит на КАЖДОЕ поступление (closes мёртвая подписка office на received).
    # Семантика: received = любое поступление (вкл. частичное); paid = полное закрытие.
    # Деньги — СТРОКОЙ (см. FIN-A2): float дрейфует копейки на собственнике.
    core.event_bus.emit(
        session,
        "finance.payment.received",
        {
            "ref": payment.ref,
            "amount": str(amt),
            "entity_ref": f"payment:{payment.id}",
            "deal_id": payment.deal_id,
            "counterparty_ref": payment.counterparty_ref,
            "outstanding": str(outstanding_after if outstanding_after > 0 else Decimal("0")),
        },
    )
    if total >= target:
        if payment.status != "paid":
            payment.status = "paid"
            payment.paid_at = datetime.now(UTC)
            core.event_bus.emit(
                session,
                "finance.payment.paid",
                {"ref": payment.ref, "entity_ref": f"payment:{payment.id}"},
            )
    elif total > 0:
        payment.status = "partial"
    await session.commit()
    await session.refresh(alloc)
    return alloc

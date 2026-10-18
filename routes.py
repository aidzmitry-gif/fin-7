"""HTTP-API модуля Finance. Монтируется под префиксом ``/finance``."""
from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.runtime.core import Core
from core.runtime.deps import get_core, get_session
from core.services.auth import get_current_user
from modules.finance.allocation import apply_allocation, sum_allocations
from modules.finance.models import (
    BankAccount,
    BankTransaction,
    Payment,
    PaymentAllocation,
)
from modules.finance.schemas import (
    AllocationCreate,
    AllocationOut,
    BankAccountCreate,
    BankAccountOut,
    BankAccountUpdate,
    BankManualMatch,
    BankTxOut,
    PaymentCreate,
    PaymentDetail,
    PaymentOut,
    StatusUpdate,
    money_str,
)
from modules.finance.summary import finance_summary

router = APIRouter(tags=["finance"])


async def _report_organization(
    organization_id: int = Query(..., gt=0),
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> int:
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Organization access service unavailable")
    await gateway.finance_member(session, organization_id, user, write=False)
    return organization_id


async def _write_organization(
    organization_id: int = Query(..., gt=0),
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
) -> int:
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Organization access service unavailable")
    await gateway.finance_member(session, organization_id, user, write=True)
    return organization_id


def _organization_report(report: dict, organization_id: int) -> dict:
    return {
        **report,
        "organization_id": organization_id,
        "provisional": True,
        "source_completeness": {
            "status": "unknown",
            "reason": "finance_source_coverage_not_confirmed",
        },
    }


@router.get("/organizations")
async def finance_organizations(
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
):
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Сервис доступа к организациям недоступен")
    candidates = await gateway.source_organizations(session, user)
    if not isinstance(candidates, list) or any(
        not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0
        or not isinstance(row.get("name"), str) or not isinstance(row.get("unp"), str)
        for row in candidates
    ) or len({row["id"] for row in candidates}) != len(candidates):
        raise HTTPException(503, "Не удалось проверить список организаций")
    result = []
    for row in sorted(candidates, key=lambda item: item["id"]):
        try:
            await gateway.finance_member(session, row["id"], user, write=False)
        except HTTPException as exc:
            if exc.status_code == 403:
                continue
            raise
        result.append({key: row[key] for key in ("id", "name", "unp")})
    return result


# ───────────────────────── Сводка / Aging / Cash-flow / Маржа / Сверка ─────────────────────────


@router.get("/summary")
async def get_summary(
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """Операционная сводка: фактическая маржа (выручка − landed − фрахт) + касса (ДДС-lite)."""
    return _organization_report(await finance_summary(session, organization_id=organization_id), organization_id)


@router.get("/aging")
async def get_aging(
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """AR/AP по корзинам срока (current/1-30/31-60/61-90/90+/без срока)."""
    from modules.finance.aging import aging_buckets

    return _organization_report(await aging_buckets(session, organization_id=organization_id), organization_id)


@router.get("/cashflow-forecast")
async def get_cashflow_forecast(
    weeks: int = 8,
    mode: str = "week",
    days: int = 30,
    account_id: int | None = Query(None, gt=0),
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """Понедельный (mode='week') или подневный (mode='day') прогноз кэш-фло (Р4).

    Опц. ``account_id`` — фильтр по банк-счёту (платежи без account_id — общий кэш,
    не суммируются в выбранный счёт). Без ``account_id`` — все платежи.
    """
    from core.services.nbrb import RateUnavailable
    from modules.finance.cashflow import FinanceAccountUnavailable, cashflow_forecast

    if mode not in ("week", "day"):
        mode = "week"
    try:
        report = await cashflow_forecast(
            session,
            weeks=max(1, min(weeks, 52)),
            days=max(1, min(days, 90)),
            mode=mode,  # type: ignore[arg-type]
            account_id=account_id,
            organization_id=organization_id,
        )
    except FinanceAccountUnavailable as exc:
        raise HTTPException(404, "Bank account unavailable in this organization") from exc
    except RateUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    return _organization_report(report, organization_id)


@router.get("/by-cost-center")
async def get_by_cost_center(
    session: AsyncSession = Depends(get_session),
    from_: str | None = None,
    to: str | None = None,
    organization_id: int = Depends(_report_organization),
):
    """Суммы расходов/доходов по центрам затрат за период (даты ISO)."""
    from modules.finance.cost_center import group_by_cost_center

    report = await group_by_cost_center(session, _safe_date(from_), _safe_date(to),
                                        organization_id=organization_id)
    return _organization_report(report, organization_id)


@router.get("/margin/by-deal")
async def margin_by_deal(
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """Маржа по сделкам: выручка − landed − чистый фрахт, сорт. по убыванию валовой."""
    from modules.finance.margin import margin_by_deal as fn

    return _organization_report(await fn(session, organization_id=organization_id), organization_id)


@router.get("/margin/by-counterparty")
async def margin_by_counterparty(
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """Маржа по контрагентам (provенанс MDM); группа «не атрибутировано» отдельно."""
    from modules.finance.margin import margin_by_counterparty as fn

    return _organization_report(await fn(session, organization_id=organization_id), organization_id)


@router.get("/margin/reconcile-deal")
async def margin_reconcile_deal(
    deal_id: int,
    items: str | None = None,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    raise HTTPException(503, 'Сверка себестоимости по организации недоступна: принадлежность источника себестоимости ещё не подтверждена.')


@router.get("/reconcile-1c")
async def reconcile_1c(
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    raise HTTPException(503, 'Сверка с 1С по организации недоступна: принадлежность источника платежей ещё не подтверждена.')


# ───────────────────────── ДДС (Р6) ─────────────────────────


@router.get("/cashflow")
async def get_cashflow(
    period_from: str | None = None,
    period_to: str | None = None,
    format: str | None = None,  # noqa: A002
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """ДДС за период (cash-basis). Все суммы — строки BYN (Decimal, без float-дрейфа).

    ``period_from`` / ``period_to`` — ISO-даты; некорректный формат → 400.
    ``?format=csv`` — CSV-выгрузка для скачивания.
    ``bank_balance`` неизвестен без подтверждённого источника выбранной организации.
    """
    from modules.finance.cashflow_dds import cashflow_report

    from_dt = _safe_date(period_from)
    to_dt = _safe_date(period_to)
    if period_from and from_dt is None:
        raise HTTPException(status_code=400, detail=f"Некорректная дата period_from: {period_from!r}")
    if period_to and to_dt is None:
        raise HTTPException(status_code=400, detail=f"Некорректная дата period_to: {period_to!r}")

    data = _organization_report(await cashflow_report(session, from_dt, to_dt, organization_id=organization_id), organization_id)

    if format == "csv":
        period_label = f"{period_from or 'начало'} — {period_to or 'сейчас'}"
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["Организация", organization_id])
        writer.writerow(["Статус", "Предварительно"])
        writer.writerow(["Полнота источников", "Не подтверждена"])
        writer.writerow(["Основание расчёта", data["calculation_basis"]])
        writer.writerow(["Показатель", "Сумма BYN", f"Период {period_label}"])
        for label, key in [
            ("Поступления", "inflows"),
            ("Выбытия", "outflows"),
            ("Нетто", "net_cashflow"),
        ]:
            writer.writerow([label, data[key]])
        writer.writerow(["Остаток на счёте", "", "Нет подтверждённых данных"] )
        writer.writerow([])
        writer.writerow(["Вид", "Сумма BYN"])
        breakdown_labels = {
            "receivable": "Поступления (receivable)",
            "payroll": "ФОТ",
            "opex": "Прочие операционные",
            "tax": "Налоги",
            "bank_fee": "Банковские расходы",
            "freight": "Фрахт",
            "landed": "Себестоимость (landed)",
            "po_planned": "PO планируемый",
        }
        for kind, amount in data["breakdown"].items():
            writer.writerow([breakdown_labels.get(kind, kind), amount])
        for limitation in data["limitations"]:
            writer.writerow(["Ограничение", "", limitation])
        csv_body = output.getvalue()
        fn = f"cashflow_{period_from or 'all'}_{period_to or 'now'}.csv"
        return Response(
            content=csv_body,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={fn}"},
        )

    return data


# ───────────────────────── Баланс (Р7) ─────────────────────────


@router.get("/balance-sheet")
async def get_balance_sheet(
    as_of: str | None = None,
    format: str | None = None,  # noqa: A002
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """Предварительный управленческий обзор, не регламентированный баланс.

    ``as_of`` — ISO-дата; некорректный формат → 400; без параметра — сегодня.
    ``?format=csv`` — CSV-выгрузка для скачивания.
    ``cash`` / ``inventory_value`` неизвестны без подтверждённого источника организации.
    """
    from modules.finance.balance_sheet import get_balance_sheet as _balance

    if as_of:
        as_of_date = _safe_date(as_of)
        if as_of_date is None:
            raise HTTPException(status_code=400, detail=f"Некорректная дата as_of: {as_of!r}")
    else:
        from datetime import date as _date

        as_of_date = _date.today()

    data = _organization_report(await _balance(session, as_of_date, organization_id=organization_id), organization_id)

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["Организация", organization_id])
        writer.writerow(["Статус", "Предварительно"])
        writer.writerow(["Полнота источников", "Не подтверждена"])
        writer.writerow(["Основание расчёта", data["calculation_basis"]])
        writer.writerow(["Предварительный управленческий обзор", "Не регламентированный баланс", data["as_of"]])
        writer.writerow(["Показатель", "Сумма BYN", "Статус"])
        for label, key in [
            ("Дебиторская задолженность", "accounts_receivable"),
            ("Денежные средства (банк)", "cash"),
            ("Запасы (склад)", "inventory_value"),
            ("ИТОГО Активы", "total_assets"),
            ("Кредиторская задолженность", "accounts_payable"),
            ("Задолженность по ФОТ", "payroll_payable"),
            ("Задолженность по налогам", "tax_payable"),
            ("ИТОГО Пассивы", "total_liabilities"),
            ("Капитал по бухгалтерским остаткам", "equity"),
        ]:
            value = data[key]
            writer.writerow([label, value if value is not None else "",
                             "Нет подтверждённых данных" if value is None else "Предварительно"])
        for limitation in data.get("limitations", []):
            writer.writerow(["Ограничение", "", limitation])
        csv_body = output.getvalue()
        fn = f"balance_sheet_{data['as_of']}.csv"
        return Response(
            content=csv_body,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={fn}"},
        )

    return data


# ───────────────────────── P&L (Р5) ─────────────────────────


@router.get("/pnl")
async def get_pnl(
    period_from: str | None = None,
    period_to: str | None = None,
    format: str | None = None,  # noqa: A002 — shadowing built-in ok для query-param
    fmt: str | None = None,  # обратная совместимость: ?fmt=csv
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
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

    data = _organization_report(await pnl_report(session, from_dt, to_dt, organization_id=organization_id), organization_id)

    csv_requested = (format or fmt) == "csv"
    if csv_requested:
        period_label = f"{period_from or 'начало'} — {period_to or 'сейчас'}"
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["Организация", organization_id])
        writer.writerow(["Статус", "Предварительно"])
        writer.writerow(["Полнота источников", "Не подтверждена"])
        writer.writerow(["Основание расчёта", data["calculation_basis"]])
        writer.writerow(["Показатель", "Сумма BYN", f"Период {period_label}"])
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
            writer.writerow([label, data[key]])
        for limitation in data["limitations"]:
            writer.writerow(["Ограничение", "", limitation])
        csv_body = output.getvalue()
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
    active_only: bool = True,
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """Счета выбранной организации (по умолчанию только активные)."""
    q = select(BankAccount).where(BankAccount.organization_id == organization_id).order_by(BankAccount.id)
    if active_only:
        q = q.where(BankAccount.is_active == 1)
    rows = (await session.execute(q)).scalars().all()
    return [BankAccountOut.model_validate(a).model_dump() for a in rows]


@router.post("/bank-accounts", response_model=BankAccountOut, status_code=201)
async def create_bank_account(
    payload: BankAccountCreate,
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_write_organization),
):
    """Завести счёт / кассу. Код уникален внутри организации."""
    if payload.organization_id != organization_id:
        raise HTTPException(422, "Организация запроса и счёта не совпадает")
    exists = (await session.execute(select(BankAccount).where(
        BankAccount.organization_id == organization_id, BankAccount.code == payload.code
    ))).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, "Счёт с таким кодом уже существует в организации")
    obj = BankAccount(
        organization_id=organization_id, code=payload.code, title=payload.title,
        currency=payload.currency, opening_balance=Decimal(str(payload.opening_balance)),
        opening_at=payload.opening_at, is_active=1,
    )
    session.add(obj)
    try:
        await session.flush()
        out = BankAccountOut.model_validate(obj).model_dump()
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Не удалось создать счёт: конфликт данных организации") from exc
    return out


@router.patch("/bank-accounts/{account_id}", response_model=BankAccountOut)
async def update_bank_account(
    account_id: int,
    payload: BankAccountUpdate,
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_write_organization),
):
    """Частичное обновление: title / opening / активность. Владелец и код неизменны."""
    obj = (await session.execute(select(BankAccount).where(
        BankAccount.id == account_id, BankAccount.organization_id == organization_id
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if obj is None:
        raise HTTPException(404, "Счёт не найден")
    data = payload.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        obj.title = data["title"]
    if "is_active" in data and data["is_active"] is not None:
        obj.is_active = 1 if data["is_active"] else 0
    if "opening_balance" in data and data["opening_balance"] is not None:
        obj.opening_balance = Decimal(str(data["opening_balance"]))
    if "opening_at" in data:
        obj.opening_at = data["opening_at"]
    await session.flush()
    out = BankAccountOut.model_validate(obj).model_dump()
    await session.commit()
    return out


# ───────────────────────── Платежи ─────────────────────────


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
        "organization_id": p.organization_id,
        "ref": p.ref,
        "amount": money_str(p.amount),
        "currency": p.currency,
        "amount_orig": money_str(p.amount_orig) if p.amount_orig is not None else None,
        "status": p.status,
        "kind": p.kind,
        "due_date": p.due_date,
        "paid_at": p.paid_at,
        "deal_id": p.deal_id,
        "counterparty_ref": p.counterparty_ref,
        "account_id": p.account_id,
        "outstanding": money_str(outstanding),
        "is_overdue": is_overdue,
        "source_controlled": bool(p.entity_ref and p.entity_ref.startswith("document:")),
    }


@router.get("/payments", response_model=list[PaymentOut])
async def list_payments(
    account_id: int | None = Query(None, gt=0),
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """Платежи организации с остатком; необязательный фильтр по её счёту."""
    if account_id is not None:
        account = (await session.execute(select(BankAccount).where(
            BankAccount.id == account_id, BankAccount.organization_id == organization_id
        ))).scalar_one_or_none()
        if account is None:
            raise HTTPException(404, "Счёт не найден")
    q = select(Payment).where(Payment.organization_id == organization_id).order_by(Payment.id.desc())
    if account_id is not None:
        q = q.where(Payment.account_id == account_id)
    rows = (await session.execute(q)).scalars().all()
    if not rows:
        return []
    sums = dict((await session.execute(
        select(PaymentAllocation.payment_id, func.sum(PaymentAllocation.amount))
        .where(PaymentAllocation.payment_id.in_([p.id for p in rows]))
        .group_by(PaymentAllocation.payment_id)
    )).all())
    today = date.today()
    return [_enrich(p, Decimal(str(sums.get(p.id, 0))), today) for p in rows]


@router.get("/payments/{payment_id}", response_model=PaymentDetail)
async def get_payment(
    payment_id: int,
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_report_organization),
):
    """Платёж организации и его частичные поступления."""
    obj = (await session.execute(select(Payment).where(
        Payment.id == payment_id, Payment.organization_id == organization_id
    ))).scalar_one_or_none()
    if obj is None:
        raise HTTPException(404, "Платёж не найден")
    allocs = (await session.execute(
        select(PaymentAllocation).where(PaymentAllocation.payment_id == payment_id)
        .order_by(PaymentAllocation.id.desc())
    )).scalars().all()
    allocated = sum((Decimal(str(a.amount)) for a in allocs), Decimal("0"))
    out = _enrich(obj, allocated)
    out["allocations"] = [AllocationOut.model_validate(a).model_dump() for a in allocs]
    return out


@router.post("/payments", response_model=PaymentOut, status_code=201)
async def create_payment(
    payload: PaymentCreate,
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_write_organization),
):
    """Зафиксировать платёж выбранной организации."""
    from core.domain.models import AuditLog
    from core.services import nbrb

    if payload.organization_id != organization_id:
        raise HTTPException(422, "Организация запроса и платежа не совпадает")
    if payload.account_id is not None:
        account = (await session.execute(select(BankAccount).where(
            BankAccount.id == payload.account_id, BankAccount.organization_id == organization_id
        ))).scalar_one_or_none()
        if account is None:
            raise HTTPException(404, "Счёт не найден")
    currency = payload.currency.strip().upper()
    if currency != "BYN" and payload.operation_date is None:
        raise HTTPException(422, "Укажите дату валютной операции")
    try:
        amount, quote = await nbrb.convert(
            session, payload.amount, currency, payload.operation_date or nbrb.today()
        )
    except nbrb.RateUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    obj = Payment(
        organization_id=organization_id, ref=payload.ref, amount=amount, currency=currency,
        amount_orig=Decimal(payload.amount) if currency != "BYN" else None,
        status=payload.status, kind=payload.kind, due_date=payload.due_date,
        deal_id=payload.deal_id, counterparty_ref=payload.counterparty_ref, account_id=payload.account_id,
    )
    session.add(obj)
    await session.flush()
    session.add(AuditLog(actor="finance", action="finance.fx.applied",
                         entity_ref=f"payment:{obj.id}",
                         detail={"amount_original": payload.amount, "amount_byn": str(amount), "quote": quote}))
    await session.flush()
    out = PaymentOut.model_validate(_enrich(obj, Decimal("0"))).model_dump()
    await session.commit()
    return out


@router.patch("/payments/{payment_id}", response_model=PaymentOut)
async def update_payment(
    payment_id: int,
    payload: StatusUpdate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_write_organization),
):
    """Сменить статус. Ручная отметка paid сохраняет прежнюю семантику."""
    obj = (await session.execute(select(Payment).where(
        Payment.id == payment_id, Payment.organization_id == organization_id
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if obj is None:
        raise HTTPException(404, "Платёж не найден")
    if obj.entity_ref and obj.entity_ref.startswith("document:"):
        raise HTTPException(409, "Статус этого требования определяется исходным счётом и поступлениями")
    obj.status = payload.status
    if payload.status == "paid":
        obj.paid_at = datetime.now(UTC)
        core.event_bus.emit(session, "finance.payment.paid", {
            "ref": obj.ref, "deal_id": obj.deal_id, "entity_ref": f"payment:{obj.id}",
            "organization_id": obj.organization_id,
        })
    await session.flush()
    allocated = await sum_allocations(session, payment_id)
    out = PaymentOut.model_validate(_enrich(obj, allocated)).model_dump()
    await session.commit()
    return out


@router.post("/payments/{payment_id}/allocations", response_model=AllocationOut, status_code=201)
async def create_allocation(
    payment_id: int,
    payload: AllocationCreate,
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_write_organization),
):
    """Частичное поступление по платежу выбранной организации."""
    payment = (await session.execute(select(Payment).where(
        Payment.id == payment_id, Payment.organization_id == organization_id
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if payment is None:
        raise HTTPException(404, "Платёж не найден")
    if payment.status in {"cancelled", "superseded"}:
        raise HTTPException(409, "Счёт отменён или заменён. Новое поступление к нему вручную не добавляется")
    amt = Decimal(str(payload.amount))
    if amt <= 0:
        raise HTTPException(400, "Сумма поступления должна быть > 0")
    from core.domain.models import AuditLog
    from core.services import nbrb

    currency = payload.currency.strip().upper()
    if currency != "BYN" and payload.operation_date is None:
        raise HTTPException(422, "Укажите дату валютного поступления")
    try:
        amt, quote = await nbrb.convert(session, amt, currency, payload.operation_date or nbrb.today())
    except nbrb.RateUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    alloc = await apply_allocation(session, core.event_bus, payment, amt)
    session.add(AuditLog(actor="finance", action="finance.fx.applied",
                         entity_ref=f"allocation:{alloc.id}",
                         detail={"amount_original": payload.amount, "amount_byn": str(amt), "quote": quote}))
    await session.flush()
    out = AllocationOut.model_validate(alloc).model_dump()
    await session.commit()
    return out


# ───────────────────────── Банк: входящие зачисления (Альфа host-to-host) ─────────────────────────


@router.post("/bank/sync")
async def bank_sync(
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    organization_id: int = Depends(_write_organization),
):
    raise HTTPException(503, 'Синхронизация банка по организации недоступна: принадлежность канала выписки ещё не подтверждена.')


@router.get("/bank/transactions", response_model=list[BankTxOut])
async def list_bank_transactions(
    status: str | None = None,
    organization_id: int = Query(..., gt=0),
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
):
    """Очередь зачислений с подтверждённой принадлежностью выбранной организации."""
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Organization access service unavailable")
    ids = await gateway.finance_bank_transaction_ids(session, organization_id, user)
    if not ids:
        return []
    q = select(BankTransaction).where(
        BankTransaction.id.in_(ids),
        BankTransaction.direction == "receipt",
        BankTransaction.source_provider.is_(None),
        BankTransaction.source_external_id.is_(None),
        BankTransaction.source_kind.is_(None),
        BankTransaction.source_reference.is_(None),
    ).order_by(BankTransaction.id.desc())
    if status:
        q = q.where(BankTransaction.match_status == status)
    rows = (await session.execute(q)).scalars().all()
    return [{**BankTxOut.model_validate(row).model_dump(), "organization_id": organization_id} for row in rows]


@router.post("/bank/transactions/{tx_id}/match", response_model=BankTxOut)
async def match_bank_transaction(
    tx_id: int,
    payload: BankManualMatch,
    organization_id: int = Query(..., gt=0),
    core: Core = Depends(get_core),
    session: AsyncSession = Depends(get_session),
    user=Depends(get_current_user),
):
    """Разнести подтверждённое зачисление на платёж той же организации."""
    gateway = getattr(core.services, "accounting", None)
    if gateway is None:
        raise HTTPException(503, "Organization access service unavailable")
    await gateway.lock_finance_bank_transaction(session, organization_id, user, tx_id)
    tx = (await session.execute(select(BankTransaction).where(
        BankTransaction.id == tx_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if tx is None:
        raise HTTPException(404, "Зачисление не найдено")
    if (tx.direction != "receipt" or any(value is not None for value in (
        tx.source_provider, tx.source_external_id, tx.source_kind, tx.source_reference,
    ))):
        raise HTTPException(409, "Операция полной выписки обрабатывается в бухгалтерии")
    if (tx.match_status != "unmatched"
            or tx.payment_id is not None or tx.allocation_id is not None):
        raise HTTPException(409, "Зачисление уже разнесено или содержит несогласованные связи")
    payment = (await session.execute(select(Payment).where(
        Payment.id == payload.payment_id, Payment.organization_id == organization_id,
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if payment is None:
        raise HTTPException(404, "Счёт не найден")
    from core.services.nbrb import RateUnavailable
    from modules.finance.official_fx import bank_amount

    try:
        amt = await bank_amount(session, tx)
    except RateUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    if amt <= 0:
        raise HTTPException(400, "У зачисления нет суммы")
    alloc = await apply_allocation(session, core.event_bus, payment, amt)
    tx.match_status = "manual"
    tx.payment_id = payment.id
    tx.allocation_id = alloc.id
    tx.note = None
    await session.flush()
    out = {**BankTxOut.model_validate(tx).model_dump(), "organization_id": organization_id}
    await session.commit()
    return out

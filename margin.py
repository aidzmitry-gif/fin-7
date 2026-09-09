"""Фактическая маржа по сделкам и контрагентам + сходимость с landed-фасадом ядра.

Группируем проводки по ``deal_id`` / ``counterparty_ref`` и считаем:
- выручка = sum(``receivable``);
- landed = sum(``landed``) − sum(``claim_refund``) — компенсация поставщика уменьшает COGS;
- net_freight = sum(``freight`` + ``freight_refund``) — возврат уменьшает фрахт;
- gross = revenue − landed − net_freight;
- pct = gross/revenue, ``None`` если выручки нет (honest-empty);
- если по группе есть выручка, но landed-проводок нет (``cogs_known=False``) — landed/gross/pct
  = ``None`` (себестоимость не атрибутирована; не показываем завышенную прибыль как COGS=0).

``po_planned`` НЕ участвует в марже (только cashflow).

Группа без deal_id/counterparty_ref — отдельная позиция «не атрибутировано»
(чтобы не размазывать неатрибутированные деньги по чужим строкам).

FIN-A1 ось A (сходимость): ``reconcile_deal_margin`` сверяет finance-landed (сумма проводок
``kind='landed'`` по deal_id) с контрольной величиной COGS из ``core.services.landed_cost``
(агрегат по sku_code позиций — landed payload без deal_id). Фасад off / нет sku — honest-empty.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment
from modules.finance.schemas import money_str

UNATTRIBUTED_DEAL = "_unattributed_"
UNATTRIBUTED_CP = "_unattributed_"

# Виды проводок, попадающие в расчёт маржи (po_planned намеренно исключён — это план, не факт)
_MARGIN_KINDS = ("receivable", "landed", "freight", "freight_refund", "claim_refund")


def _row(key, agg: dict[str, Decimal]) -> dict:
    revenue = agg["receivable"]
    net_freight = agg["freight"] + agg["freight_refund"]
    # Себестоимость атрибутирована к этой группе, только если есть landed-проводки.
    # В проде landed payload идёт БЕЗ deal_id (контракт «PO обслуживает много сделок»),
    # поэтому по сделке landed структурно = 0. Показывать gross=revenue−0−freight как валовую
    # прибыль = фантомная прибыль (себестоимость выпадает) — PLATFORM #1. Честнее: gross/pct/landed
    # = None + флаг cogs_known=False, чтобы UI показал «себестоимость неизвестна», а не завышенный %.
    cogs_known = agg["landed"] > 0
    # Гард только для реальных сделок/контрагентов; бакет «не атрибутировано» (key=None) и так
    # явно помечен в UI и не претендует на маржу конкретной сделки.
    if key is not None and revenue > 0 and not cogs_known:
        return {
            "key": key,
            "revenue": money_str(revenue),
            "landed": None,
            "freight": money_str(net_freight),
            "gross": None,
            "pct": None,
            "cogs_known": False,
        }
    landed = agg["landed"] - agg["claim_refund"]  # компенсация уменьшает COGS
    gross = revenue - landed - net_freight
    pct = float(gross / revenue * 100) if revenue > 0 else None  # pct — процент, НЕ деньги
    return {
        "key": key,
        "revenue": money_str(revenue),
        "landed": money_str(landed),
        "freight": money_str(net_freight),
        "gross": money_str(gross),
        "pct": pct,
        "cogs_known": True,
    }


async def _grouped(session: AsyncSession, group_field) -> list[dict]:
    rows = (await session.execute(select(Payment))).scalars().all()
    by: dict[object, dict[str, Decimal]] = {}
    for p in rows:
        if p.kind not in _MARGIN_KINDS:
            continue  # po_planned/прочее — не в маржу
        key = getattr(p, group_field) if getattr(p, group_field) not in (None, "") else None
        slot = by.setdefault(
            key,
            {
                "receivable": Decimal("0"),
                "landed": Decimal("0"),
                "freight": Decimal("0"),
                "freight_refund": Decimal("0"),
                "claim_refund": Decimal("0"),
            },
        )
        if p.kind in slot:
            slot[p.kind] += Decimal(str(p.amount))
    result = [_row(k if k is not None else None, v) for k, v in by.items()]
    # сортируем по убыванию валовой прибыли; строки без известного COGS (gross=None) и
    # неатрибутированные (key=None) — в конец. gross — строка BYN или None.
    result.sort(
        key=lambda r: (
            r["key"] is None,
            r["gross"] is None,
            -Decimal(r["gross"]) if r["gross"] is not None else Decimal("0"),
        )
    )
    return result


async def margin_by_deal(session: AsyncSession) -> dict:
    rows = await _grouped(session, "deal_id")
    return {"currency": "BYN", "items": rows}


async def margin_by_counterparty(session: AsyncSession) -> dict:
    rows = await _grouped(session, "counterparty_ref")
    return {"currency": "BYN", "items": rows}


def _parse_items(raw: str | None) -> dict[str, Decimal]:
    """Распарсить query-параметр ``items=SKU1:qty1,SKU2:qty2`` → {sku: qty}.

    Пустая строка / некорректный токен → пропускаем (honest-empty, не падаем).
    """
    if not raw:
        return {}
    out: dict[str, Decimal] = {}
    for tok in raw.split(","):
        if ":" not in tok:
            continue
        sku, qty = tok.split(":", 1)
        sku = sku.strip()
        if not sku:
            continue
        try:
            q = Decimal(qty.strip())
        except (InvalidOperation, ValueError):
            continue
        out[sku] = q
    return out


async def reconcile_deal_margin(
    session: AsyncSession,
    landed_facade,
    deal_id: int,
    items: dict[str, Decimal] | None = None,
) -> dict:
    """Сходимость landed по сделке: finance↔facade (FIN-A1).

    finance_landed = сумма проводок ``kind='landed'`` по ``deal_id`` (минус компенсация
    по претензии не вычитаем — сверка идёт с фасадом landed-cost, который тоже не знает
    про претензии; компенсация уже отдельной строкой в summary/by-deal).

    facade_landed = СУММА(unit_landed_cost_byn × qty) по позициям ``items`` (sku → qty),
    взятая через ``core.services.landed_cost.last_landed_cost_batch`` (агрегат по SKU,
    без deal_id — landed payload без сделки). Если фасад None или ``items`` пуст —
    honest-empty: ``source_facade_available=False``, ``facade_landed=None``, ``delta=None``.

    Возврат::

        {deal_id, finance_landed, facade_landed, delta, revenue, gross_finance,
         level:'sku_aggregate', source_facade_available}
    """
    # выручка по сделке (для контекста UI — расчёт gross на стороне finance)
    revenue_row = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.deal_id == deal_id, Payment.kind == "receivable"
            )
        )
    ).scalar_one()
    revenue = Decimal(str(revenue_row))

    finance_landed_row = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.deal_id == deal_id, Payment.kind == "landed"
            )
        )
    ).scalar_one()
    finance_landed = Decimal(str(finance_landed_row))

    # фасадная контрольная величина: unit×qty по списку SKU (агрегат, не per-deal)
    facade_landed: Decimal | None = None
    source_facade_available = False
    if landed_facade is not None and items:
        try:
            batch = await landed_facade.last_landed_cost_batch(session, list(items.keys()))
            total = Decimal("0")
            any_hit = False
            for sku, qty in items.items():
                rec = batch.get(sku) if isinstance(batch, dict) else None
                if not rec:
                    continue
                unit = Decimal(str(rec.get("unit_landed_cost_byn") or 0))
                total += unit * qty
                any_hit = True
            if any_hit:
                facade_landed = total
                source_facade_available = True
        except Exception:  # noqa: BLE001 — fail-soft: фасад не упадёт в UI
            facade_landed = None
            source_facade_available = False

    delta = (
        (finance_landed - facade_landed)
        if (facade_landed is not None)
        else None
    )
    # чистый freight за сделку — для контекстного gross
    net_freight_row = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.deal_id == deal_id, Payment.kind.in_(("freight", "freight_refund"))
            )
        )
    ).scalar_one()
    net_freight = Decimal(str(net_freight_row))
    claim_row = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.deal_id == deal_id, Payment.kind == "claim_refund"
            )
        )
    ).scalar_one()
    claim_refund = Decimal(str(claim_row))
    gross_finance = revenue - (finance_landed - claim_refund) - net_freight

    return {
        "deal_id": deal_id,
        "finance_landed": money_str(finance_landed),
        "facade_landed": (money_str(facade_landed) if facade_landed is not None else None),
        "delta": (money_str(delta) if delta is not None else None),
        "revenue": money_str(revenue),
        "gross_finance": money_str(gross_finance),
        "level": "sku_aggregate",
        "source_facade_available": source_facade_available,
        "currency": "BYN",
    }

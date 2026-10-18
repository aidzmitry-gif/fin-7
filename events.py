"""Реакции модуля Finance на события других модулей (через шину, §2.5).

Finance не знает о sales/procurement/logistics напрямую — реагирует на доменное событие
из шины и пишет в свою таблицу через контекст доставки (``EventContext``).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from modules.finance.fx import BASE as BASE_CCY
from modules.finance.models import LANDED_PROJECTION_PREFIX, Payment
from modules.finance.official_fx import event_amount


def _parse_due_date(raw) -> date | None:
    """Принять ISO-строку или date; вернуть date или None (honest-empty)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None  # ponytail: подгрузим валидацию когда продажи начнут слать срок


def _to_decimal(raw) -> Decimal:
    """Безопасный Decimal: None/мусор → 0 (honest-empty)."""
    if raw is None or raw == "":
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


async def on_document_posted(payload: dict, ctx) -> None:
    """Счёт выпущен в ERP → создаём платёж к оплате (sales → finance).

    Лайфсайкл (0061): ``due_date`` из payload (по умолчанию None — honest-empty),
    ``deal_id``/``counterparty_ref`` — провенанс связи для маржи by-deal/by-counterparty.

    FIN-A2 (P3): payload ``finance.payment.created`` несёт ``amount`` СТРОКОЙ —
    деньги собственника не должны проходить через float (дрейф копеек).
    """
    if payload.get("kind") != "invoice" or ctx is None:
        return
    amount = _to_decimal(payload.get("amount"))
    currency = (payload.get("currency") or BASE_CCY).strip().upper()
    amount_byn = await event_amount(amount, currency, payload, ctx)
    ctx.session.add(
        Payment(
            ref=payload.get("number", ""),
            amount=amount_byn,
            currency=currency,
            amount_orig=amount if currency != BASE_CCY else None,
            status="pending",
            due_date=_parse_due_date(payload.get("due_date")),
            deal_id=payload.get("deal_id"),
            counterparty_ref=payload.get("counterparty_ref"),
        )
    )
    ctx.services.event_bus.emit(
        ctx.session,
        "finance.payment.created",
        {
            "ref": payload.get("number"),
            "amount": str(amount_byn),
            "deal_id": payload.get("deal_id"),
            "entity_ref": payload.get("entity_ref"),
        },
    )


async def on_freight_cost(payload: dict, ctx) -> None:
    """Доставка завершена → расход на перевозку (logistics → finance).

    Пишем платёж ``kind="freight"`` (расход), чтобы доход (счета) и фрахт можно было
    развести при подсчёте валовой прибыли. Нулевой/пустой тариф игнорируем.
    Импорт-плечо (leg='import') приходит без ``deal_id`` — терпим.
    """
    if ctx is None:
        return
    amount = _to_decimal(payload.get("amount"))
    if amount <= 0:
        return
    ref = payload.get("ref") or payload.get("entity_ref") or ""
    currency = (payload.get("currency") or BASE_CCY).upper()
    amount_byn = await event_amount(amount, currency, payload, ctx)
    ctx.session.add(
        Payment(
            ref=f"freight:{ref}",
            amount=amount_byn,
            amount_orig=amount if currency != BASE_CCY else None,
            currency=currency,
            status="pending",
            kind="freight",
            deal_id=payload.get("deal_id"),
            counterparty_ref=payload.get("counterparty_ref"),
        )
    )


async def on_freight_refund(payload: dict, ctx) -> None:
    """Аудит счёта перевозчика выявил переплату → возврат (logistics → finance).

    Логистика эмитит ``logistics.freight.audit_refund`` при ``variance > 0`` (счёт
    перевозчика больше тарифа). Это деньги к возврату — кредит против расхода на фрахт,
    поэтому пишем ``kind="freight_refund"`` с **отрицательной** суммой: тогда
    ``sum(amount)`` по фрахт-платежам даёт чистый фрахт, а ``kind`` хранит аудит-след.

    FIN-C2 (P3): шаблон FX — ``currency`` из payload (дефолт BYN), ``amount_byn`` через
    официальный курс на дату события; ``amount_orig`` сохраняем при не-BYN.
    Хранится отрицательной. Нет курса → ошибка, исходное событие остаётся для повтора.
    """
    if ctx is None:
        return
    amount = _to_decimal(payload.get("amount"))
    if amount <= 0:
        return
    ref = payload.get("entity_ref") or payload.get("shipment_code") or ""
    currency = (payload.get("currency") or BASE_CCY).upper()
    amount_byn = await event_amount(amount, currency, payload, ctx)
    ctx.session.add(
        Payment(
            ref=f"freight_refund:{ref}",
            amount=-amount_byn,  # отрицательная — кредит против фрахта
            amount_orig=amount if currency != BASE_CCY else None,
            currency=currency,
            status="pending",
            kind="freight_refund",
            counterparty_ref=payload.get("counterparty_ref"),
        )
    )


async def on_landed_cost(payload: dict, ctx) -> None:
    """Project the exact received-PO calculation. Cost never attests payment."""
    org, order_id = payload.get("organization_id"), payload.get("purchase_order_id")
    if (ctx is None or payload.get("stage") != "actual"
            or any(type(x) is not int or x <= 0 for x in (org, order_id, getattr(ctx, "event_id", None)))):
        raise ValueError("Landed cost requires its exact owned calculation receipt and delivery context")
    if getattr(ctx.services, "procurement_source", None) is None:
        raise ValueError("Verified purchase calculation source is unavailable")
    await ctx.services.accounting.lock_event_organization(ctx.session, org)
    source = await ctx.services.procurement_source.landed_financial_source(
        ctx.session, org, order_id, ctx.event_id,
    )
    if (source["organization_id"] != org or source["purchase_order_id"] != order_id
            or source["event_id"] != ctx.event_id or source["payload"] != payload
            or source["currency"] != "BYN"):
        raise ValueError("Landed event does not match its verified source")
    # Reuse the same transaction serialization as immutable invoice projections.
    from modules.finance.document_versions import _serialize

    await _serialize(ctx.session)
    sku_hash = hashlib.sha256(source["sku_code"].encode()).hexdigest()
    identity = f"{LANDED_PROJECTION_PREFIX}{source['receipt_id']}:{order_id}:{sku_hash}"
    binding = "landed-v1:" + hashlib.sha256(json.dumps(
        source, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()
    amount = Decimal(source["amount"])
    if (not amount.is_finite() or amount < 0 or amount >= Decimal("1000000000000")
            or amount != amount.quantize(Decimal("0.01"))):
        raise ValueError("Exact finite BYN landed total is required")
    ref = f"landed:purchase_order:{order_id}:{source['sku_code']}"
    rows = (await ctx.session.scalars(select(Payment).where(
        (Payment.entity_ref == identity) | (Payment.ref == ref) |
        ((Payment.kind == "landed") & (Payment.ref == f"landed:purchase_order:{order_id}")),
    ).with_for_update().execution_options(populate_existing=True))).all()
    if rows:
        if len(rows) != 1:
            raise ValueError("Landed cost has ambiguous legacy projections; review required")
        row = rows[0]
        if (row.organization_id != org or row.entity_ref != identity or row.description != binding
                or row.ref != ref
                or row.amount != amount or row.currency != "BYN" or row.amount_orig is not None
                or row.kind != "landed" or row.status != "recorded" or row.paid_at is not None
                or row.account_id is not None or row.deal_id is not None or row.counterparty_ref is not None
                or row.due_date is not None):
            raise ValueError("Landed cost conflicts with its immutable source; review required")
        from modules.finance.allocation import sum_allocations

        if await sum_allocations(ctx.session, row.id):
            raise ValueError("Cost projection cannot contain payment allocations")
        return
    ctx.session.add(Payment(
        organization_id=org, ref=ref,
        amount=amount, amount_orig=None, currency="BYN", status="recorded", kind="landed",
        entity_ref=identity, description=binding,
    ))
    await ctx.session.flush()


async def on_claim_resolved(payload: dict, ctx) -> None:
    """Закупки урегулировали претензию к поставщику → компенсация-приток (procurement → finance).

    Подписка на ``procurement.claim.resolved``. При ``status='resolved'`` и положительной
    ``amount_byn`` пишем ``Payment(kind='claim_refund', amount=+amount_byn, ...)`` — приток от
    поставщика против landed-затрат. ``rejected``/None/ноль — игнор.

    ⚠ Статус урегулирования — в поле ``status`` ('resolved'/'rejected'), а НЕ в ``resolution``
    (это свободный текст «как урегулировано», ``SupplierClaim.resolution``). Сверять по ``status``,
    иначе на реальном событии закупок приток молча теряется (деньги собственника, PLATFORM #1).

    ⚠ Контракт-фриз: ``amount_byn`` приходит СТРОКОЙ и УЖЕ В BYN — НЕ конвертируем через FX
    (double-convert = порча денег). ``supplier_id`` (int) — ручка контрагента (нет UNP/MDM).
    """
    if ctx is None:
        return
    if payload.get("status") != "resolved":
        return
    amount = _to_decimal(payload.get("amount_byn"))
    if amount <= 0:
        return
    supplier_id = payload.get("supplier_id")
    entity_ref = payload.get("entity_ref") or f"claim:{payload.get('claim_id', '')}"
    ref = f"claim:{entity_ref}"
    # Идемпотентность (S1): шина at-least-once → повтор procurement.claim.resolved НЕ должен
    # задваивать приток-компенсацию (деньги собственника, PLATFORM #1). Ключ — ref + kind.
    from sqlalchemy import select
    if (
        await ctx.session.execute(
            select(Payment.id).where(Payment.ref == ref, Payment.kind == "claim_refund")
        )
    ).scalars().first() is not None:
        return
    ctx.session.add(
        Payment(
            ref=ref,
            amount=amount,  # положительная — приток-компенсация
            status="pending",
            kind="claim_refund",
            counterparty_ref=str(supplier_id) if supplier_id is not None else None,
        )
    )


async def on_reference_changed(payload: dict, ctx) -> None:
    """Справочники сменили ставку/мастер-поле SKU → пометить landed-проводки как stale.

    Подписка на ``reference.sku.changed`` / ``reference.ref_tnved.changed`` /
    ``reference.ref_vat_rate.changed`` / ``reference.ref_currency_rate.changed`` (B2 Круг 4,
    парная работа с Закупками). Финансы — **единый писатель проводок**, поэтому НЕ пишем
    напрямую: эмитим аутгоинг-сигнал ``finance.landed.recompute_requested`` (outbox-паттерн),
    Закупки подписываются и переэмитят ``procurement.landed_cost.calculated`` с актуальными
    мастер-входами; финансы тогда перепишут проводки штатным ``on_landed_cost``.

    **Дебаунс/идемпотентность.** На каждое входящее reference-событие выпускаем РОВНО ОДИН
    сигнал, ``sku_codes`` дедуплицирован (set → sorted list). Если SKU нет — ``payments=0``,
    сигнал не эмитим (honest-empty). Для FX-курса (``core.currency_rates``) сигнал содержит
    ``currency_code`` вместо ``sku_codes`` (затронуты ВСЕ не-BYN landed-проводки).

    Контекст пересчёта: через ``core.services.sku_master.landed_inputs_batch`` подгружаем
    актуальные мастер-входы (duty/vat/weight/volume) и кладём в payload — Закупки используют
    как «снимок» вместо повторного резолва.
    """
    if ctx is None:
        return
    ref_key = payload.get("ref_key") or ""
    entity_ref = payload.get("entity_ref") or ""
    actor = payload.get("actor")
    table_value = entity_ref.split(":", 1)[1] if ":" in entity_ref else ""

    # FX-курс — отдельный канал (SKU-агрегата нет; затронуты ВСЕ не-BYN landed-проводки)
    if ref_key == "core.currency_rates":
        ctx.services.event_bus.emit(
            ctx.session,
            "finance.fx.recompute_requested",
            {
                "ref_key": ref_key,
                "entity_ref": entity_ref,
                "currency_code": table_value,
                "actor": actor,
            },
        )
        return

    # Резолвим затронутые SKU по типу справочника (только прямые ссылки Sku.tnved_code/vat_code;
    # эффективный код через nomenclature_group — # ponytail: подключим, когда появится граф групп).
    from sqlalchemy import select

    from core.domain.models import Sku

    sku_codes: set[str] = set()
    if ref_key == "core.skus":
        # entity_ref = "sku:<code>" → один SKU
        if table_value:
            sku_codes.add(table_value)
    elif ref_key == "core.tnved" and table_value:
        rows = (
            await ctx.session.execute(
                select(Sku.code).where(Sku.tnved_code == table_value)
            )
        ).all()
        sku_codes.update(c for (c,) in rows)
    elif ref_key == "core.vat_rates" and table_value:
        rows = (
            await ctx.session.execute(
                select(Sku.code).where(Sku.vat_code == table_value)
            )
        ).all()
        sku_codes.update(c for (c,) in rows)
    else:
        return  # незнакомый справочник — пропустить (honest-empty, не падать)

    if not sku_codes:
        return  # затронутых SKU нет — сигнал не эмитим (honest-empty)

    codes_sorted = sorted(sku_codes)
    # Контекст пересчёта: актуальные landed_inputs через ГОТОВЫЙ фасад ядра (REF3-1)
    inputs: dict[str, dict | None] = {}
    facade = getattr(ctx.services, "sku_master", None)
    if facade is not None and hasattr(facade, "landed_inputs_batch"):
        try:
            inputs = await facade.landed_inputs_batch(ctx.session, codes_sorted)
        except Exception:  # noqa: BLE001 — fail-soft: пересчёт можно и без контекста
            inputs = {}

    ctx.services.event_bus.emit(
        ctx.session,
        "finance.landed.recompute_requested",
        {
            "ref_key": ref_key,
            "entity_ref": entity_ref,
            "sku_codes": codes_sorted,
            "actor": actor,
            "inputs": inputs,
        },
    )


async def on_po_drafted(payload: dict, ctx) -> None:
    """Закупки выписали PO → планируемый отток (procurement → finance).

    Подписка на ``procurement.po.drafted``. Пишем ``Payment(kind='po_planned', status='planned')``
    с ``due_date=eta_date`` и ``counterparty_ref=str(supplier_id)`` — это ещё НЕ обязательство
    (не AR/AP), но cashflow-прогноз учитывает (FIN-B1).

    ⚠ Контракт-фриз: ``planned_amount`` — строка в BYN, ``currency='BYN'``, ``deal_id=null``.
    Finance не создаёт PO — только проецирует будущий отток.
    """
    if ctx is None:
        return
    amount = _to_decimal(payload.get("planned_amount"))
    if amount <= 0:
        return
    po_ref = payload.get("po_ref") or ""
    supplier_id = payload.get("supplier_id")
    ctx.session.add(
        Payment(
            ref=f"po:{po_ref}",
            amount=amount,
            status="planned",
            kind="po_planned",
            due_date=_parse_due_date(payload.get("eta_date")),
            counterparty_ref=str(supplier_id) if supplier_id is not None else None,
        )
    )


# ─────────────────────── HR: ФОТ ───────────────────────


async def on_payroll_accrued(payload: dict, ctx) -> None:
    """HR начислил зарплату → платёж-обязательство (hr → finance).

    Подписка на ``hr.payroll.accrued``. Контракт зафиксирован HR 2026-06-28, НЕ менять форму:
    ``{employee_id, employee_name, period:"YYYY-MM", amount_byn:str(BYN), entity_ref:"payroll:<id>"}``.
    Пишем ``Payment(kind='payroll', status='pending')`` — ФОТ отражается как обязательство-отток.
    """
    if ctx is None:
        return
    amount = _to_decimal(payload.get("amount_byn"))
    if amount <= 0:
        return
    entity_ref = payload.get("entity_ref", "")
    employee_name = payload.get("employee_name", "")
    period = payload.get("period", "")
    ctx.session.add(
        Payment(
            ref=entity_ref,
            amount=amount,
            status="pending",
            kind="payroll",
            description=f"ФОТ {employee_name} {period}",
            entity_ref=entity_ref,
        )
    )


async def on_payroll_paid(payload: dict, ctx) -> None:
    """HR выплатил зарплату → закрыть платёж по entity_ref (hr → finance).

    Подписка на ``hr.payroll.paid``. Контракт: ``{employee_id, period, amount_byn, entity_ref}``.
    Находим Payment по ``entity_ref`` и ``kind='payroll'`` → переводим в ``paid``.
    Не найден → пропустить (honest-empty, HR может быть быстрее finance).
    """
    if ctx is None:
        return
    from sqlalchemy import select

    entity_ref = payload.get("entity_ref", "")
    if not entity_ref:
        return
    row = (
        await ctx.session.execute(
            select(Payment).where(
                Payment.entity_ref == entity_ref,
                Payment.kind == "payroll",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return  # honest-empty: accrued не пришло раньше paid
    row.status = "paid"


# ─────────────────────── Sales: признание выручки (accrual) ───────────────────────


async def on_deal_handoff(payload: dict, ctx) -> None:
    """Сделка отгружена → признание выручки (accrual по отгрузке, sales → finance).

    Подписка на ``sales.deal.handoff``. Payload несёт ``amount`` (float, sell-total BYN);
    оборачиваем ``Decimal(str())`` — float-drift недопустим для выручки собственника.

    kind=``revenue_recognized`` — ОТДЕЛЬНЫЙ от ``receivable`` (cash-invoice):
    summary.py и aging работают с ``receivable`` (cash-basis), P&L — с ``revenue_recognized``
    (accrual-basis). Смешение = двойной счёт выручки.

    Идемпотентность: повтор handoff по той же сделке НЕ задваивает проводку
    (entity_ref="deal:<deal_id>" + kind="revenue_recognized").

    ⚠ ACCRUAL подтверждён собственником 2026-06-28.
    """
    if ctx is None:
        return
    deal_id = payload.get("deal_id")
    amount = _to_decimal(payload.get("amount"))
    if amount <= 0 or deal_id is None:
        return

    from sqlalchemy import select

    entity_ref = f"deal:{deal_id}"
    existing = (
        await ctx.session.execute(
            select(Payment).where(
                Payment.entity_ref == entity_ref,
                Payment.kind == "revenue_recognized",
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return  # идемпотентность: один handoff → одна проводка выручки

    number = payload.get("number", "")
    ctx.session.add(
        Payment(
            ref=f"handoff:{deal_id}",
            amount=amount,
            status="pending",
            kind="revenue_recognized",
            deal_id=deal_id,
            entity_ref=entity_ref,
            description=f"Выручка сделка {number}",
        )
    )

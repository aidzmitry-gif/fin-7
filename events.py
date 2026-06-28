"""Реакции модуля Finance на события других модулей (через шину, §2.5).

Finance не знает о sales/procurement/logistics напрямую — реагирует на доменное событие
из шины и пишет в свою таблицу через контекст доставки (``EventContext``).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from modules.finance.fx import BASE as BASE_CCY
from modules.finance.fx import UnknownCurrency, to_byn
from modules.finance.models import Payment


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
    """Счёт записан в 1С → создаём платёж к оплате (sales → finance).

    Лайфсайкл (0061): ``due_date`` из payload (по умолчанию None — honest-empty),
    ``deal_id``/``counterparty_ref`` — провенанс связи для маржи by-deal/by-counterparty.

    FIN-A2 (P3): payload ``finance.payment.created`` несёт ``amount`` СТРОКОЙ —
    деньги собственника не должны проходить через float (дрейф копеек).
    """
    if payload.get("kind") != "invoice" or ctx is None:
        return
    amount = _to_decimal(payload.get("amount"))
    ctx.session.add(
        Payment(
            ref=payload.get("number", ""),
            amount=amount,
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
            "amount": str(amount),  # FIN-A2: деньги — строкой, не float
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
    try:
        amount_byn = to_byn(amount, currency)
    except UnknownCurrency:
        return  # неизвестная валюта → не падать в relay, пропустить (honest-empty)
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
    fx.to_byn (с буфером для не-BYN), ``amount_orig`` сохраняем при не-BYN. Хранится
    отрицательной. Неизвестная валюта → пропускаем (не падаем в relay).
    """
    if ctx is None:
        return
    amount = _to_decimal(payload.get("amount"))
    if amount <= 0:
        return
    ref = payload.get("entity_ref") or payload.get("shipment_code") or ""
    currency = (payload.get("currency") or BASE_CCY).upper()
    try:
        amount_byn = to_byn(amount, currency)
    except UnknownCurrency:
        return  # неизвестная валюта → не падать в relay
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
    """Закупка зафиксировала себестоимость прихода → проводка-затрата (procurement → finance).

    Подписка на ``procurement.landed_cost.calculated``. Себестоимость прихода = расход
    (``kind="landed"``) для подсчёта фактической маржи: выручка − landed − фрахт.
    Сумму берём готовой (``amount`` либо ``total_landed_byn``) либо считаем ``unit×qty``.
    Нуль игнорим (honest-empty).

    ⚠ Закупки P4 могут менять ``stage`` 'estimated'→'actual' — ЭТОТ обработчик ``stage``
    НЕ читает (только qty/total/unit), так что P4 безопасно приземляется.
    """
    if ctx is None:
        return
    amount = _to_decimal(payload.get("amount"))
    if amount <= 0:
        amount = _to_decimal(payload.get("total_landed_byn"))
    if amount <= 0:
        amount = _to_decimal(payload.get("unit_landed_cost_byn")) * _to_decimal(payload.get("qty"))
    if amount <= 0:
        return
    ref = payload.get("entity_ref") or payload.get("sku_code") or ""
    currency = (payload.get("currency") or BASE_CCY).upper()
    try:
        amount_byn = to_byn(amount, currency)
    except UnknownCurrency:
        return
    ctx.session.add(
        Payment(
            ref=f"landed:{ref}",
            amount=amount_byn,
            amount_orig=amount if currency != BASE_CCY else None,
            currency=currency,
            status="pending",
            kind="landed",
            deal_id=payload.get("deal_id"),
            counterparty_ref=payload.get("counterparty_ref"),
        )
    )


async def on_claim_resolved(payload: dict, ctx) -> None:
    """Закупки урегулировали претензию к поставщику → компенсация-приток (procurement → finance).

    Подписка на ``procurement.claim.resolved``. При ``resolution='resolved'`` и положительной
    ``amount_byn`` пишем ``Payment(kind='claim_refund', amount=+amount_byn, ...)`` — приток от
    поставщика против landed-затрат. ``rejected``/None/ноль — игнор.

    ⚠ Контракт-фриз: ``amount_byn`` приходит СТРОКОЙ и УЖЕ В BYN — НЕ конвертируем через FX
    (double-convert = порча денег). ``supplier_id`` (int) — ручка контрагента (нет UNP/MDM).
    """
    if ctx is None:
        return
    if payload.get("resolution") != "resolved":
        return
    amount = _to_decimal(payload.get("amount_byn"))
    if amount <= 0:
        return
    supplier_id = payload.get("supplier_id")
    entity_ref = payload.get("entity_ref") or f"claim:{payload.get('claim_id', '')}"
    ctx.session.add(
        Payment(
            ref=f"claim:{entity_ref}",
            amount=amount,  # положительная — приток-компенсация
            status="pending",
            kind="claim_refund",
            counterparty_ref=str(supplier_id) if supplier_id is not None else None,
        )
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

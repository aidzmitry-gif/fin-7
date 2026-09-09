"""Импорт входящих зачислений из банка + матчинг к счетам (Альфа host-to-host, слайс 1).

Поток: ``core.services.bank.fetch_incoming`` → дедуп по ``ext_id`` (идемпотентность) →
матчер (назначение платежа + УНП) → авто-проводка поступления ЕДИНЫМ путём
(``allocation.apply_allocation`` → событие ``finance.payment.received`` → офис в «Оплачено»).
Несматченное — в очередь ``match_status='unmatched'`` («разобрать вручную»).

⚠ Деньги (PLATFORM #1): суммы — Decimal из строки (не float); авто-проводим ТОЛЬКО при
однозначном совпадении. Любая неоднозначность (несколько кандидатов / переплата / УНП не
совпал) → очередь, а не догадка. Фабриковать зачисления нельзя — источник без кредов даёт [].
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.domain.models import Counterparty
from modules.finance.allocation import apply_allocation, sum_allocations
from modules.finance.models import BankTransaction, Payment

# Открытый счёт к получению = ждёт денег от клиента.
_OPEN_STATUSES = ("pending", "partial")


def _to_decimal(raw) -> Decimal | None:
    """Строка/число суммы → Decimal; мусор → None (не выдумываем сумму)."""
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw))
        return value if value.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _digits(raw: str | None) -> str:
    return re.sub(r"\D", "", raw or "")


def _ref_in_purpose(ref: str, purpose_upper: str) -> bool:
    """Номер счёта фигурирует в назначении платежа.

    Матч по значимому «хвосту» ссылки: буквенно-цифровой токен длиной ≥ 3 (например
    ``СЧ-100`` → ищем ``СЧ100`` в очищенном назначении). Короткие/пустые ``ref`` — не матчим
    (ложные срабатывания на деньгах недопустимы).
    """
    key = re.sub(r"[^0-9A-Za-zА-Яа-я]", "", ref or "").upper()
    return len(key) >= 3 and key in purpose_upper


async def _resolve_payment_unp(session: AsyncSession, payment: Payment) -> str | None:
    """Лучшая догадка об УНП плательщика по счёту (для подтверждения матча).

    ``counterparty_ref`` — свободная строка (может быть УНП, имя или id). Порядок:
    1) если это 9 цифр — считаем УНП напрямую;
    2) иначе резолвим контрагента по имени в shared-kernel → его ``unp``.
    None — УНП не восстановлен (тогда матч держится на назначении, УНП не блокирует).
    """
    ref = (payment.counterparty_ref or "").strip()
    if not ref:
        return None
    d = _digits(ref)
    # ref — «голый» УНП, если после удаления цифр остаётся лишь метка «УНП» и пунктуация
    # («191234567» или «УНП-191234567»), но не имя контрагента («ООО … 191234567»).
    if len(d) == 9:
        residue = re.sub(r"[0-9]", "", ref).upper().replace("УНП", "").strip(" -:.")
        if residue == "":
            return d
    row = (
        await session.execute(select(Counterparty).where(Counterparty.name == ref))
    ).scalar_one_or_none()
    return (row.unp or None) if row is not None else None


async def _match_candidate(
    session: AsyncSession, tx: dict, amount: Decimal
) -> tuple[Payment | None, str]:
    """Найти единственный открытый счёт под зачисление. Возврат (payment|None, причина).

    Правила авто-матча (консервативно — деньги):
      1. кандидаты = открытые ``receivable`` (pending/partial) с остатком > 0, чей номер
         фигурирует в назначении платежа;
      2. если у зачисления есть УНП и у счёта УНП восстановлен и НЕ совпал — кандидат отпадает;
      3. авто-матч ТОЛЬКО если остался ровно ОДИН кандидат и сумма ≤ остатку.
    Иначе — причина для очереди.
    """
    purpose_upper = re.sub(r"[^0-9A-Za-zА-Яа-я]", "", tx.get("purpose") or "").upper()
    if not purpose_upper:
        return None, "пустое назначение платежа"
    payer_unp = _digits(tx.get("payer_unp"))

    rows = (
        await session.execute(
            select(Payment).where(
                Payment.kind == "receivable", Payment.status.in_(_OPEN_STATUSES)
            )
        )
    ).scalars().all()

    candidates: list[tuple[Payment, Decimal]] = []
    unp_rejected = False
    for p in rows:
        if not _ref_in_purpose(p.ref, purpose_upper):
            continue
        outstanding = Decimal(str(p.amount)) - await sum_allocations(session, p.id)
        if outstanding <= 0:
            continue
        if payer_unp:
            p_unp = _digits(await _resolve_payment_unp(session, p))
            if p_unp and p_unp != payer_unp:
                unp_rejected = True
                continue
        candidates.append((p, outstanding))

    if not candidates:
        return None, ("УНП плательщика не совпал со счётом" if unp_rejected
                      else "счёт по назначению не найден")
    if len(candidates) > 1:
        return None, f"несколько кандидатов ({len(candidates)}) — нужен ручной выбор"
    payment, outstanding = candidates[0]
    if amount > outstanding:
        return None, f"сумма {amount} больше остатка {outstanding} — ручной разбор"
    return payment, "matched"


async def sync_incoming(session: AsyncSession, gateway, event_bus, since=None) -> dict:
    """Опросить банк, провести идемпотентно, сматчить. Возврат — сводка по итогам.

    Идемпотентность: уже виденные ``ext_id`` пропускаем (не задваиваем зачисление).
    Транзакцию НЕ коммитим — граница у роута (``POST /finance/bank/sync``).
    """
    if gateway is None:
        return {"source_available": False, "fetched": 0, "new": 0, "matched": 0, "unmatched": 0}

    incoming = await gateway.fetch_incoming(since)
    fetched = len(incoming)

    known = set(
        (await session.execute(select(BankTransaction.ext_id))).scalars().all()
    )
    new = matched = unmatched = 0
    for tx in incoming:
        ext_id = str(tx.get("ext_id") or "").strip()
        if not ext_id or ext_id in known:
            continue  # без ext_id провести идемпотентно нельзя — пропускаем (не задваиваем)
        known.add(ext_id)
        new += 1
        amount = _to_decimal(tx.get("amount"))
        row = BankTransaction(
            ext_id=ext_id,
            occurred_on=_parse_date(tx.get("date")),
            amount=amount if amount is not None else Decimal("0"),
            currency=(tx.get("currency") or "BYN")[:3],
            payer_unp=(tx.get("payer_unp") or None),
            payer_name=(tx.get("payer_name") or None),
            purpose=(tx.get("purpose") or None),
            account_code=(tx.get("account_code") or None),
        )
        if amount is None or amount <= 0:
            row.match_status = "unmatched"
            row.note = "нет суммы зачисления"
            session.add(row)
            unmatched += 1
            continue
        from modules.finance.official_fx import bank_amount

        amount = await bank_amount(session, row)
        payment, reason = await _match_candidate(session, tx, amount)
        if payment is not None:
            alloc = await apply_allocation(session, event_bus, payment, amount)
            row.match_status = "matched"
            row.payment_id = payment.id
            row.allocation_id = alloc.id
            matched += 1
        else:
            row.match_status = "unmatched"
            row.note = reason
            unmatched += 1
        session.add(row)

    return {
        "source_available": True,
        "fetched": fetched,
        "new": new,
        "matched": matched,
        "unmatched": unmatched,
    }


def _parse_date(raw):
    from datetime import date

    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None

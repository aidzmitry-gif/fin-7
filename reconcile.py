"""Сверка платежей ERP с 1С — СТРОГО ЧТЕНИЕ, fail-soft.

1С — ledger; здесь только читаем (``OneCGateway.fetch_payments``) и сравниваем с
``finance.payment``. Если шлюз не настроен или метод не реализован — отдаём
honest-empty с флагом ``source_available=False`` (НЕ ошибка, экран показывает плашку).

Не пишем в 1С, ledger не дублируем.

⚠ Круг 5 харднинг (К5-1): сопоставление через **список-по-ключу**, не словарь —
иначе дубли `ref+counterparty_ref` (исправления, пересчёты, отсутствие УНП у пары
платежей с одинаковым ref) схлопываются и теряются. Сумма парсится **безопасно**
(локализация «100,00» из 1С + Decimal от строки + fail-soft 0 на мусор), float
сейчас в выходе остался (money-в-API — отдельный NEEDS-ARB на круге 5).
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment


def _erp_key(p: Payment) -> str:
    """Ключ группировки: ref (счёт) + контрагент (если есть). Используется только для bucket'a;
    несколько платежей с одинаковым ключом — это нормально (дубли, исправления, пересчёт)."""
    cp = p.counterparty_ref or ""
    return f"{p.ref}|{cp}"


def _onec_key(row: dict) -> str:
    return f"{row.get('ref', '')}|{row.get('counterparty_ref', '') or ''}"


def _safe_amount(raw) -> float:
    """Безопасный parse суммы из 1С: int/float как есть, строка — через Decimal с заменой
    запятой на точку (РФ-локализация 1С). Мусор / None → 0.0 (fail-soft, не исключение)."""
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        s = str(raw).replace(" ", "").replace(" ", "").replace(",", ".")
        return float(Decimal(s))
    except (InvalidOperation, ValueError, TypeError):
        return 0.0


async def reconcile_with_onec(session: AsyncSession, gateway) -> dict:
    """Сопоставить платежи ERP с тем, что вернул 1С.

    Возвращает ``{matched, only_in_erp, only_in_1c, as_of, source, source_available}``.
    При недоступности 1С (нет шлюза / нет метода / падение / пусто) — source_available=False.

    Матчинг — **по парам в bucket'е ключа** (Round-5 К5-1): для каждого ключа берём
    min(len(erp), len(onec)) пар → matched; остаток ERP → only_in_erp; остаток 1С →
    only_in_1c. Дубли НЕ схлопываются. Внутри bucket'а сопоставляем по индексу (стабильно
    по порядку записи), без эвристик по сумме — это сверка наличия, не амт-матчинг.
    """
    from datetime import UTC, datetime

    as_of = datetime.now(UTC).isoformat()
    onec_rows: list[dict] = []
    source_available = False
    if gateway is not None and hasattr(gateway, "fetch_payments"):
        try:
            onec_rows = list(await gateway.fetch_payments())
            source_available = True
        except Exception:  # noqa: BLE001 — fail-soft по контракту 1С (см. CLAUDE.md финансы)
            onec_rows = []
            source_available = False

    erp_payments = (await session.execute(select(Payment))).scalars().all()
    erp_buckets: dict[str, list[Payment]] = defaultdict(list)
    for p in erp_payments:
        erp_buckets[_erp_key(p)].append(p)
    onec_buckets: dict[str, list[dict]] = defaultdict(list)
    for r in onec_rows:
        onec_buckets[_onec_key(r)].append(r)

    matched: list[dict] = []
    only_in_erp: list[dict] = []
    only_in_1c: list[dict] = []
    all_keys = set(erp_buckets) | set(onec_buckets)
    for k in all_keys:
        erp_list = erp_buckets.get(k, [])
        onec_list = onec_buckets.get(k, [])
        pair_count = min(len(erp_list), len(onec_list))
        for p in erp_list[:pair_count]:
            matched.append(
                {"ref": p.ref, "amount": float(p.amount), "counterparty_ref": p.counterparty_ref}
            )
        for p in erp_list[pair_count:]:
            only_in_erp.append(
                {"ref": p.ref, "amount": float(p.amount), "counterparty_ref": p.counterparty_ref}
            )
        for r in onec_list[pair_count:]:
            only_in_1c.append(
                {
                    "ref": r.get("ref"),
                    "amount": _safe_amount(r.get("amount", 0)),
                    "counterparty_ref": r.get("counterparty_ref"),
                }
            )

    return {
        "as_of": as_of,
        "source": "1c",
        "source_available": source_available,
        "matched": matched,
        "only_in_erp": only_in_erp,
        "only_in_1c": only_in_1c,
    }

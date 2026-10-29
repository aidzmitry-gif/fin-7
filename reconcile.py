"""Сверка платежей ERP с 1С — СТРОГО ЧТЕНИЕ, fail-soft.

1С — ledger; здесь только читаем (``OneCGateway.fetch_payments``) и сравниваем с
``finance.payment``. Если шлюз не настроен или метод не реализован — отдаём
honest-empty с флагом ``source_available=False`` (НЕ ошибка, экран показывает плашку).

Не пишем в 1С, ledger не дублируем.

⚠ Круг 5 харднинг (К5-1): сопоставление через **список-по-ключу**, не словарь —
иначе дубли `ref+counterparty_ref` (исправления, пересчёты, отсутствие УНП у пары
платежей с одинаковым ref) схлопываются и теряются. Сумма парсится через Decimal с
поддержкой локализации «100,00»; некорректные значения выводятся отдельно. Суммы в выходе —
**строки BYN** (`money_str`): деньги собственника не через float (приоритет №1 PLATFORM.md).
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment
from modules.finance.schemas import money_str


def _erp_key(p: Payment) -> tuple[str, str]:
    """Ключ группировки: ref (счёт) + контрагент (если есть). Используется только для bucket'a;
    несколько платежей с одинаковым ключом — это нормально (дубли, исправления, пересчёт)."""
    cp = p.counterparty_ref or ""
    return p.ref, cp


def _onec_key(row: dict) -> tuple[str, str]:
    return row["ref"], row.get("counterparty_ref") or ""


def _source_amount(raw) -> Decimal | None:
    """Parse exact finite monetary amounts without substituting invalid values."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        s = str(raw).replace(" ", "").replace(" ", "").replace(",", ".")
        value = Decimal(s)
        if not value.is_finite() or value != value.quantize(Decimal("0.01")):
            return None
        return value
    except (InvalidOperation, ValueError, TypeError):
        return None


async def reconcile_with_onec(session: AsyncSession, gateway) -> dict:
    """Сопоставить платежи ERP с тем, что вернул 1С.

    Возвращает ``{matched, only_in_erp, only_in_1c, as_of, source, source_available}``.
    При недоступности 1С (нет шлюза / нет метода / отказ источника / падение) —
    source_available=False; пустой ответ работающего шлюза допустим.

    Совпадение требует одинаковых реквизитов и точной суммы. Дубли учитываются отдельно;
    неоднозначные остатки остаются по сторонам, неподходящие суммы не становятся нулём.
    """
    from datetime import UTC, datetime

    as_of = datetime.now(UTC).isoformat()
    onec_rows: list[dict] = []
    source_available = False
    source_declined = gateway is not None and getattr(gateway, "financial_source_available", None) is False
    if gateway is not None and not source_declined and hasattr(gateway, "fetch_payments"):
        try:
            onec_rows = list(await gateway.fetch_payments())
            source_available = True
        except Exception:  # noqa: BLE001 — fail-soft по контракту 1С (см. CLAUDE.md финансы)
            onec_rows = []
            source_available = False

    erp_payments = (await session.execute(select(Payment).order_by(Payment.id))).scalars().all()
    erp_buckets: dict[tuple[str, str], list[Payment]] = defaultdict(list)
    for p in erp_payments:
        erp_buckets[_erp_key(p)].append(p)
    onec_buckets: dict[tuple[str, str], list[tuple[dict, Decimal]]] = defaultdict(list)
    invalid_source_rows: list[dict] = []
    for r in onec_rows:
        if not isinstance(r, dict):
            invalid_source_rows.append({"ref": None, "counterparty_ref": None, "reason": "invalid_row"})
            continue
        ref, cp = r.get("ref"), r.get("counterparty_ref")
        if not isinstance(ref, str) or not ref.strip() or (cp is not None and not isinstance(cp, str)):
            invalid_source_rows.append({"ref": ref if isinstance(ref, str) else None,
                                        "counterparty_ref": cp if isinstance(cp, str) else None,
                                        "reason": "invalid_identity"})
            continue
        amount = _source_amount(r.get("amount"))
        if amount is None:
            invalid_source_rows.append({"ref": ref, "counterparty_ref": cp, "reason": "invalid_amount"})
            continue
        onec_buckets[_onec_key(r)].append((r, amount))

    matched: list[dict] = []
    amount_mismatch: list[dict] = []
    only_in_erp: list[dict] = []
    only_in_1c: list[dict] = []
    all_keys = dict.fromkeys([*erp_buckets, *onec_buckets])
    for k in all_keys:
        remaining_erp = []
        remaining_onec = list(onec_buckets.get(k, []))
        for p in erp_buckets.get(k, []):
            index = next((i for i, (_, amount) in enumerate(remaining_onec) if amount == p.amount), None)
            if index is None:
                remaining_erp.append(p)
                continue
            remaining_onec.pop(index)
            matched.append({"ref": p.ref, "amount": money_str(p.amount), "counterparty_ref": p.counterparty_ref})
        if len(remaining_erp) == len(remaining_onec) == 1:
            p, (_, amount) = remaining_erp.pop(), remaining_onec.pop()
            amount_mismatch.append({"ref": p.ref, "counterparty_ref": p.counterparty_ref,
                                    "erp_amount": money_str(p.amount), "one_c_amount": money_str(amount)})
        for p in remaining_erp:
            only_in_erp.append(
                {"ref": p.ref, "amount": money_str(p.amount), "counterparty_ref": p.counterparty_ref}
            )
        for r, amount in remaining_onec:
            only_in_1c.append(
                {
                    "ref": r.get("ref"),
                    "amount": money_str(amount),
                    "counterparty_ref": r.get("counterparty_ref"),
                }
            )

    result = {
        "as_of": as_of,
        "source": "1c",
        "source_available": source_available,
        "matched": matched,
        "amount_mismatch": amount_mismatch,
        "invalid_source_rows": invalid_source_rows,
        "only_in_erp": only_in_erp,
        "only_in_1c": only_in_1c,
    }
    if source_declined:
        result["source_reason"] = getattr(gateway, "financial_source_reason", None)
    return result

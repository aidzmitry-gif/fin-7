"""Сверка платежей ERP с 1С — СТРОГО ЧТЕНИЕ, fail-soft.

1С — ledger; здесь только читаем (``OneCGateway.fetch_payments``) и сравниваем с
``finance.payment``. Если шлюз не настроен или метод не реализован — отдаём
honest-empty с флагом ``source_available=False`` (НЕ ошибка, экран показывает плашку).

Не пишем в 1С, ledger не дублируем.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.finance.models import Payment


def _erp_key(p: Payment) -> str:
    """Ключ сопоставления: ref (счёт) + контрагент (если есть)."""
    cp = p.counterparty_ref or ""
    return f"{p.ref}|{cp}"


def _onec_key(row: dict) -> str:
    return f"{row.get('ref', '')}|{row.get('counterparty_ref', '') or ''}"


async def reconcile_with_onec(session: AsyncSession, gateway) -> dict:
    """Сопоставить платежи ERP с тем, что вернул 1С.

    Возвращает ``{matched, only_in_erp, only_in_1c, as_of, source, source_available}``.
    При недоступности 1С (нет шлюза / нет метода / падение / пусто) — source_available=False.
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

    erp = (await session.execute(select(Payment))).scalars().all()
    erp_by_key = {_erp_key(p): p for p in erp}
    onec_by_key = {_onec_key(r): r for r in onec_rows}

    matched, only_in_erp, only_in_1c = [], [], []
    for k, p in erp_by_key.items():
        if k in onec_by_key:
            matched.append({"ref": p.ref, "amount": float(p.amount), "counterparty_ref": p.counterparty_ref})
        else:
            only_in_erp.append({"ref": p.ref, "amount": float(p.amount), "counterparty_ref": p.counterparty_ref})
    for k, r in onec_by_key.items():
        if k not in erp_by_key:
            only_in_1c.append(
                {
                    "ref": r.get("ref"),
                    "amount": float(r.get("amount", 0)),
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

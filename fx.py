"""Pure conversion with an explicitly supplied rate; fetch dated quotes via nbrb."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

BASE = "BYN"


class UnknownCurrency(ValueError):
    """Валюта не в таблице курсов — лучше упасть честно, чем сохранить мусор."""


def to_byn(amount: Decimal | float | str | int, currency: str | None, *, rate=None) -> Decimal:
    """Convert at the supplied BYN-per-unit rate, without a planning buffer."""
    amt = Decimal(str(amount))
    cur = (currency or BASE).upper()
    if cur == BASE:
        return amt
    if rate is None:
        raise UnknownCurrency(f"Для {cur} нужен официальный курс на дату из core.services.nbrb")
    rate = Decimal(str(rate))
    if not rate.is_finite() or rate <= 0:
        raise UnknownCurrency("Некорректный курс")
    return (amt * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

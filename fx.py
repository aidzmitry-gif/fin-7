"""FX-конвертация для мультивалютных входящих проводок.

Базовая валюта хранения и расчётов — **BYN**. Если событие приходит в иной валюте,
конвертируем в BYN с буфером (методика «Расчёт Китай»: +10% к курсу — страховка от
дрейфа на горизонте поставки).

``RATES`` — demo-константы (BYN-эквивалент за единицу). ⚠ Источник истины — НБРБ/1С;
этот словарь — заглушка до интеграции (# ponytail: подключить курс из 1С/НБРБ-API).
"""
from __future__ import annotations

from decimal import Decimal

BASE = "BYN"
FX_BUFFER = Decimal("1.10")  # +10% — согласовано с методикой Китая (docs/landed-cost.md)

# ponytail: значения — demo; реальный курс должен приходить из 1С/НБРБ.
RATES: dict[str, Decimal] = {
    "BYN": Decimal("1.0"),
    "USD": Decimal("3.30"),
    "CNY": Decimal("0.45"),
    "EUR": Decimal("3.60"),
    "RUB": Decimal("0.035"),
}


class UnknownCurrency(ValueError):
    """Валюта не в таблице курсов — лучше упасть честно, чем сохранить мусор."""


def to_byn(amount: Decimal | float | str | int, currency: str | None) -> Decimal:
    """Сконвертировать сумму в BYN с применением FX-буфера.

    BYN/None → возвращаем как есть (Decimal). Иначе ``amount × rate × buffer``.
    """
    amt = Decimal(str(amount))
    cur = (currency or BASE).upper()
    if cur == BASE:
        return amt
    rate = RATES.get(cur)
    if rate is None:
        raise UnknownCurrency(f"Курс для {cur} не задан — обновите fx.RATES или включите шлюз НБРБ")
    return (amt * rate * FX_BUFFER).quantize(Decimal("0.01"))

"""Financial operations use dated official rates without a planning buffer."""
from datetime import UTC, date
from decimal import Decimal
from zoneinfo import ZoneInfo

from core.domain.models import AuditLog
from core.services.nbrb import RateUnavailable, convert


async def event_amount(amount, currency, payload, ctx) -> Decimal:
    if currency == "BYN":
        return Decimal(str(amount))
    raw = payload.get("operation_date") or payload.get("document_date")
    if raw:
        on = date.fromisoformat(str(raw))
    elif getattr(ctx, "occurred_at", None) is not None:
        occurred = ctx.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=UTC)
        on = occurred.astimezone(ZoneInfo("Europe/Minsk")).date()
    else:
        raise RateUnavailable("Валютная операция требует дату операции или дату исходного события")
    value, rate = await convert(ctx.session, amount, currency, on)
    ctx.session.add(AuditLog(
        actor="finance", action="finance.fx.applied",
        entity_ref=str(payload.get("entity_ref") or payload.get("ref") or payload.get("number") or "")[:64],
        detail={"amount_original": str(amount), "amount_byn": str(value), "quote": rate},
    ))
    return value


async def bank_amount(session, tx) -> Decimal:
    currency = (tx.currency or "BYN").strip().upper()
    if currency == "BYN":
        return Decimal(str(tx.amount))
    if tx.occurred_on is None:
        raise RateUnavailable("Валютное зачисление требует дату банка")
    value, rate = await convert(session, tx.amount, currency, tx.occurred_on)
    session.add(AuditLog(actor="finance", action="finance.fx.applied",
                         entity_ref=f"bank:{tx.ext_id}"[:64],
                         detail={"amount_original": str(tx.amount), "amount_byn": str(value),
                                 "quote": rate}))
    return value

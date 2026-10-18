"""Projection of immutable invoice versions; no bank ingestion or matching logic.

Use the existing Finance producer for legacy events. New originals are keyed by
exact document ID. Replacements retire outstanding demand, never allocations.
"""
import hashlib
import json
from decimal import Decimal

from sqlalchemy import select, text, update

from modules.finance.events import on_document_posted as legacy_document_posted
from modules.finance.models import Payment
from modules.finance.official_fx import event_amount


async def _serialize(session):
    if session.get_bind().dialect.name == 'postgresql':
        await session.execute(text('SELECT pg_advisory_xact_lock(1129467202)'))
    else:
        await session.execute(update(Payment).where(Payment.id == -1).values(id=Payment.id))


async def on_original_issued(payload, ctx):
    if not payload.get('content_sha256'):
        await legacy_document_posted(payload, ctx)
        return
    if ctx is None or payload.get('kind') != 'invoice':
        return
    if not payload.get('document_id') or not isinstance(payload.get('amount'), str):
        raise ValueError('Immutable invoice requires a stable ID and exact money')
    await _serialize(ctx.session)
    ref = f"document:{payload['document_id']}"
    rows = (await ctx.session.execute(select(Payment).where(Payment.entity_ref == ref))).scalars().all()
    if rows:
        if len(rows) != 1 or rows[0].amount != Decimal(payload['amount']) or rows[0].ref != payload['number']:
            raise ValueError('Invoice projection conflicts with the saved original')
        return
    # Keep the legacy writer's event contract, then attach the exact version identity
    # in the same transaction. Finance remains the sole writer of Payment.
    await legacy_document_posted(payload, ctx)
    pending = [row for row in ctx.session.new if isinstance(row, Payment) and row.ref == payload['number']]
    if len(pending) != 1:
        raise ValueError('Invoice projection did not create exactly one payment')
    pending[0].entity_ref = ref
    pending[0].currency = payload.get('currency', 'BYN')
    pending[0].description = f"Версия {payload['document_version']}; документ #{payload['document_id']}"


async def on_original_superseded(payload, ctx):
    if ctx is None or payload.get('kind') != 'invoice':
        return
    await _serialize(ctx.session)
    ref = f"document:{payload['document_id']}"
    # A row lock alone does not refresh an invoice already held in the identity map.
    rows = (await ctx.session.execute(select(Payment).where(Payment.entity_ref == ref)
        .with_for_update().execution_options(populate_existing=True))).scalars().all()
    if not rows:
        # Explicit legacy reference only, with original persisted amount/deal check.
        rows = (await ctx.session.execute(select(Payment).where(
            Payment.ref == payload['number'], Payment.deal_id == payload['deal_id'],
            Payment.kind == 'receivable',
        ).with_for_update().execution_options(populate_existing=True))).scalars().all()
    if not rows:
        if payload.get('content_sha256'):
            raise ValueError('Original invoice projection is not available yet; retry replacement')
        return  # Legacy records may never have been projected into Finance.
    if len(rows) != 1 or rows[0].amount != Decimal(payload['amount']):
        raise ValueError('Superseded invoice has ambiguous financial identity; review required')
    old = rows[0]
    description = f"Заменён документом #{payload['replacement_document_id']}; оплаты сохранены здесь"
    if old.description == description and old.status in {'paid', 'superseded'}:
        return
    old.description = description
    if old.status != 'paid':
        old.status = 'superseded'
    ctx.services.event_bus.emit(ctx.session, 'finance.invoice.superseded', {
        'document_id': payload['document_id'], 'payment_id': old.id,
        'replacement_document_id': payload['replacement_document_id'],
        'payment_transfer': False, 'entity_ref': f'payment:{old.id}',
    })


def _binding(source, byn):
    # Application replay binding, not a database immutability guarantee. Current
    # lifecycle state is deliberately excluded: cancellation preserves money.
    body = {key: source[key] for key in (
        "document_id", "deal_id", "organization_id", "version", "content_sha256",
        "issuance_receipt", "number", "amount", "currency", "document_date",
    )}
    body["amount_byn"] = format(Decimal(byn), ".2f")
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "invoice-v1:" + hashlib.sha256(encoded.encode()).hexdigest()


async def _payment(session, source):
    # Also detect pre-existing unbound legacy demand. Never assign an inferred
    # owner or silently create a second row beside it.
    rows = (await session.scalars(select(Payment).where(
        (Payment.entity_ref == f"document:{source['document_id']}") |
        ((Payment.ref == source["number"]) & (Payment.deal_id == source["deal_id"])),
    ).order_by(Payment.id).with_for_update().execution_options(populate_existing=True))).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("Invoice demand is ambiguous; reconciliation required")
    row = rows[0]
    original = Decimal(source["amount"])
    if (row.organization_id != source["organization_id"] or row.kind != "receivable"
            or row.entity_ref != f"document:{source['document_id']}"
            or row.ref != source["number"] or row.deal_id != source["deal_id"]
            or row.currency != source["currency"]
            or (row.amount_orig != original if source["currency"] != "BYN"
                else row.amount != original or row.amount_orig is not None)
            or row.description != _binding(source, row.amount)):
        raise ValueError("Invoice demand binding conflicts; reconciliation required")
    return row


def _retire(row, status):
    # Allocations and paid_at remain on the original. A late cancellation must
    # not overwrite the stronger, already verified replacement state.
    if row is not None and row.status not in {"paid", "superseded"}:
        row.status = status


async def _invoice_event(payload, ctx, *, cancelled):
    if ctx is None:
        raise ValueError("Invoice event requires its transactional service context")
    source_api = ctx.services.sales_source
    document_id, organization_id = payload.get("document_id"), payload.get("organization_id")
    if (type(document_id) is not int or document_id <= 0
            or type(organization_id) is not int or organization_id <= 0
            or await source_api.invoice_organization(ctx.session, document_id) != organization_id):
        raise ValueError("Invoice event organization does not match")
    await ctx.services.accounting.lock_event_organization(ctx.session, organization_id)
    source = await source_api.invoice_financial_lifecycle(
        ctx.session, document_id, organization_id=organization_id,
        expected_version=payload.get("document_version"),
        expected_content_sha256=payload.get("content_sha256"),
    )
    if payload.get("deal_id", source["deal_id"]) != source["deal_id"]:
        raise ValueError("Invoice event deal does not match")
    if cancelled:
        receipt = source["cancellation"]
        if receipt is None or any(payload.get(key) != receipt[field] for key, field in (
            ("cancellation_id", "cancellation_id"), ("cancellation_digest", "digest"),
            ("release_id", "release_id"),
        )):
            raise ValueError("Invoice cancellation event does not match its receipt")
    elif (payload.get("amount") != source["amount"] or payload.get("currency") != source["currency"]
            or payload.get("number") != source["number"]
            or payload.get("issuance_receipt") != source["issuance_receipt"]):
        raise ValueError("Invoice issuance event does not match its original")
    if not source["collectible"] and source["cancellation"] is None:
        raise ValueError("Terminal invoice lacks confirmed cancellation; reconciliation required")
    await _serialize(ctx.session)
    row = await _payment(ctx.session, source)
    previous = source["predecessor"]
    if previous is not None:
        _retire(await _payment(ctx.session, previous), "superseded")
    if not source["collectible"]:
        _retire(row, "cancelled")
        return
    if cancelled:
        raise ValueError("Cancellation cannot leave collectible demand")
    if row is not None:
        return  # Validate saved binding before FX; historical rates never replay.
    amount = await event_amount(Decimal(source["amount"]), source["currency"], {
        "document_date": source["document_date"], "entity_ref": f"document:{document_id}",
    }, ctx)
    if (not amount.is_finite() or amount < 0 or amount >= Decimal("1000000000000")
            or amount != amount.quantize(Decimal("0.01"))):
        raise ValueError("Converted invoice amount is invalid")
    ctx.session.add(Payment(
        organization_id=organization_id, ref=source["number"], amount=amount,
        currency=source["currency"],
        amount_orig=Decimal(source["amount"]) if source["currency"] != "BYN" else None,
        kind="receivable", status="pending", deal_id=source["deal_id"],
        entity_ref=f"document:{document_id}", description=_binding(source, amount),
    ))
    await ctx.session.flush()


async def on_invoice_issued(payload, ctx):
    """Verified ERP invoice demand only; no payment or revenue event."""
    await _invoice_event(payload, ctx, cancelled=False)


async def on_invoice_cancelled(payload, ctx):
    await _invoice_event(payload, ctx, cancelled=True)

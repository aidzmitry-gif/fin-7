"""Projection of immutable invoice versions; no bank ingestion or matching logic.

Use the existing Finance producer for legacy events. New originals are keyed by
exact document ID. Replacements retire outstanding demand, never allocations.
"""
from decimal import Decimal

from sqlalchemy import select, text, update

from modules.finance.events import on_document_posted as legacy_document_posted
from modules.finance.models import Payment


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

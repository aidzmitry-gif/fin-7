# Модуль finance — контекст для Claude

**Тип:** git submodule → fin-7 (правка = коммит в этот репозиторий)
**API-префикс:** `/finance`
**Схема БД:** `finance`
**Статус:** ТЗ-Р3 (Круг 3) закрыт — операционная сводка + lifecycle + сходимость маржи

## Назначение
Учёт платежей по фактам-проводкам: счёт→оплата, фрахт, landed-себес, компенсация по
претензии, планируемый PO. Замыкает цепочку «счёт из sales → платёж в finance → отметка
об оплате обратно в sales» и проецирует затратные/будущие события закупок/логистики в
кассу+маржу. Ledger/НДС/ЭСЧФ ОСТАЮТСЯ в 1С — здесь только операционный срез.

## Файлы
- `module.py` — `FinanceModule(ModuleContract)`, фабрика `get_module()`.
- `models.py` — ORM `Payment` (схема `finance`).
- `routes.py` — HTTP-API (`/finance/*`); монтируется ядром.
- `events.py` — обработчики: `on_document_posted`, `on_freight_cost`, `on_freight_refund`,
  `on_landed_cost`, **`on_claim_resolved`** (Р3), **`on_po_drafted`** (Р3).
- `summary.py` — `finance_summary(session)`: маржа (по фактам) + касса (ДДС-lite).
- `margin.py` — маржа by-deal/by-counterparty + **`reconcile_deal_margin`** (Р3 ось A).
- `aging.py` — AR/AP по корзинам срока.
- `cashflow.py` — понедельный прогноз (учитывает **po_planned** — Р3).
- `cost_center.py` — центры затрат (учитывает **claim_refund**, исключает **po_planned**).
- `fx.py` — конвертация в BYN + буфер +10%.
- `reconcile.py` — сверка с 1С (СТРОГО чтение).
- `schemas.py` — Pydantic.

## Что регистрирует в ядре (register())
- **Роуты**: `core.include_router(routes.router, prefix="/finance")`.
- **Подписки**:
  - `sales.document.posted` → `on_document_posted` (kind=invoice → `Payment(kind=receivable, status=pending)`).
  - `logistics.freight.cost` → `on_freight_cost` (доставлено → `Payment(kind=freight)`; импорт-плечо без deal_id терпим).
  - `logistics.freight.audit_refund` → `on_freight_refund` (переплата → `Payment(kind=freight_refund)`, **отрицательная**; FX-шаблон с буфером — Р3).
  - `procurement.landed_cost.calculated` → `on_landed_cost` (себестоимость прихода → `Payment(kind=landed)`).
  - **`procurement.claim.resolved` → `on_claim_resolved`** (Р3): resolved + amount_byn>0 → `Payment(kind=claim_refund)`.
  - **`procurement.po.drafted` → `on_po_drafted`** (Р3): PO выписан → `Payment(kind=po_planned, status=planned)` для прогноза кассы.
- **Widgets**: `Widget("finance", "Финансы", source="finance.payments")`.

## События
- **Публикует**:
  - `finance.payment.created` — из `on_document_posted` при создании платежа из счёта. **`amount` — СТРОКА** (Р3, FIN-A2: деньги собственника не через float). payload `{ref, amount:str, deal_id, entity_ref}`.
  - `finance.payment.paid` — из `PATCH /payments/{id}` (status=paid) и `POST /allocations` при полном закрытии. payload `{ref, entity_ref:"payment:<id>"}`.
  - **`finance.payment.received`** (Р3, FIN-C3) — из `POST /payments/{id}/allocations` на **каждое** поступление (вкл. частичное; для office, замыкает мёртвую подписку). payload `{ref, amount:str, entity_ref, deal_id, counterparty_ref, outstanding:str}`.
- **Подписан на**: см. блок «Подписки» выше.

## Модель данных
- `finance.payment` (`Payment`): `id`, `ref(255)`, `amount(Numeric 14,2)` — **всегда в BYN**,
  `status(32)` (`planned`→`pending`→`partial`→`paid`), `kind(32)`, `created_at`.
  Lifecycle (0061): `due_date`, `paid_at`, `deal_id`, `counterparty_ref(64)`.
  Мультивалюта (0063): `cost_center(32)`, `currency(3)`, `amount_orig(Numeric)`.
- `finance.payment_allocation`: `id`, `payment_id FK→payment.id ON DELETE CASCADE`, `amount`, `allocated_at`.

## Виды проводок (`kind`)
| kind | Знак | Смысл |
|---|---|---|
| `receivable` | + | Счёт к получению (sales) |
| `freight` | + | Расход на перевозку (logistics) |
| `freight_refund` | − | Возврат переплаты перевозчиком (logistics, кредит против фрахта) |
| `landed` | + | Себестоимость прихода (procurement) |
| `claim_refund` | + | **Р3**: компенсация поставщика по урегулированной претензии (procurement); уменьшает чистый landed |
| `po_planned` | + | **Р3**: планируемый отток по выписанному PO (procurement); в cashflow, **НЕ в маржу/aging** |

## API-эндпоинты
- `GET /finance/summary` — операционная сводка: маржа (по фактам, с учётом claim_refund) +
  касса ДДС-lite + затраты по типам.
- `GET /finance/aging` — AR/AP по корзинам; `po_planned`/`claim_refund` НЕ участвуют.
- `GET /finance/cashflow-forecast?weeks=N` — понедельная проекция; **отток включает `po_planned`** (Р3).
- `GET /finance/by-cost-center?from=&to=` — суммы по центрам; `claim_refund` → доход в центр Закупки; `po_planned` исключён.
- `GET /finance/margin/by-deal`, `GET /finance/margin/by-counterparty` — маржа (с учётом claim_refund вычитанием из landed); `po_planned` исключён.
- **`GET /finance/margin/reconcile-deal?deal_id=&items=SKU1:qty1,SKU2:qty2`** (Р3, FIN-A1) — сходимость
  finance↔facade landed по сделке. Без `items` или без `core.services.landed_cost` →
  `source_facade_available=false` (honest-empty), `delta=None`, `finance_landed` всё равно отдаётся.
- `GET /finance/reconcile-1c` — сверка с 1С (СТРОГО ЧТЕНИЕ, fail-soft). Метод `OneCGateway.fetch_payments` (см. ниже).
- `GET /finance/payments` — список платежей (вычисляемые `outstanding`/`is_overdue`).
- `GET /finance/payments/{id}` — платёж + allocations + outstanding.
- `POST /finance/payments` — зафиксировать платёж.
- `PATCH /finance/payments/{id}` — сменить статус; при `paid` — `paid_at=now()` + эмит `finance.payment.paid`.
- `POST /finance/payments/{id}/allocations` — частичное поступление; эмит **`finance.payment.received` на каждое** + `finance.payment.paid` при полном закрытии.

## Lifecycle платежа (миграция 0061)
- `status`: `planned` → `pending` → `partial` → `paid` (свободная строка).
- **`overdue` НЕ хранится** — вычисляется на чтении: `status in (pending, partial) and due_date < today`.
- Авто-смена в `POST /allocations`: `sum(amount) ≥ amount` → `paid` + эмит paid; `0 < sum < amount` → `partial`. На **каждое** поступление — эмит `received`.

## Мультивалюта + центры затрат (миграция 0063)
- `amount` хранится ВСЕГДА в BYN после конвертации через `fx.py` (rates demo + буфер +10%).
- `cost_center` — свободная строка; справочник захардкожен (`landed/po_planned/claim_refund → Закупки, freight/refund → Логистика, receivable → Продажи`).
- **Р3 FIN-C2**: `on_freight_refund` теперь использует шаблон FX (currency из payload, amount_byn через `to_byn` с буфером, `amount_orig` сохраняется при не-BYN; `UnknownCurrency` → не падать, пропустить).

## Сходимость маржи finance↔facade (Р3, FIN-A1)
`reconcile_deal_margin(session, landed_facade, deal_id, items)`:
- `finance_landed` = `sum(amount where kind='landed' and deal_id=:id)`.
- `facade_landed` = `Σ (unit_landed_cost_byn × qty)` по позициям `items` через `core.services.landed_cost.last_landed_cost_batch` — **агрегат по SKU** (landed payload без deal_id).
- `delta = finance_landed − facade_landed`. Фасад None / items пуст / падение → `source_facade_available=false`, `facade_landed=None`, `delta=None`; finance всё равно отдаётся (honest).
- Возвращает `revenue`/`gross_finance` для контекста UI.

## Контракт-фриз потребляемых событий
- **`procurement.claim.resolved`**: `amount_byn` — **СТРОКА, УЖЕ в BYN** + `supplier_id:int`. **НЕ конвертировать через FX** (double-convert = порча денег). `order_id` опционален.
- **`procurement.po.drafted`**: `{po_ref, supplier_id:int, planned_amount:str(BYN), currency:'BYN', eta_date:str|None, deal_id:null}`.
- **`procurement.landed_cost.calculated`**: НЕ несёт `deal_id` — реконсиляция на уровне SKU-агрегата. P4 закупок переключает `stage` (estimated↔actual) — `on_landed_cost` НЕ читает `stage`, поэтому P4 безопасно приземляется.
- **`logistics.freight.cost`** импорт-плечо: `leg='import'`, без `deal_id` — обрабатываем.

## Хотспот ядра — `core/services/onec.py` (Р3, FIN-C4)
`OneCGateway.fetch_payments(self) -> list[dict]` добавлен аддитивно — СТРОГО ЧТЕНИЕ, нужен
для `/finance/reconcile-1c`. Реализатор (полоса Синк/integrations) может вернуть `[]` —
fail-soft на стороне `finance.reconcile` остался. **Согласовать при открытии полосы Синк.**

## Подводные камни / детали
- `POST /payments` сам коммитит сессию — отступление от паттерна (учитывать при изменениях).
- `on_document_posted` пишет через `ctx.session` и эмитит через `ctx.services.event_bus` — коммит делает доставщик outbox.
- `amount` в API — `float` (схемы); в ORM — `Decimal`; **в событии `finance.payment.created`/`finance.payment.received` — `str`** (точность денег собственника).
- Статусы — свободные строки (нет enum); особые: `paid` → событие `finance.payment.paid`; `planned` → НЕ участвует в AR/AP overdue.
- **Единый писатель** в схему `finance.*` — только этот модуль; ledger/НДС/ЭСЧФ ОСТАЮТСЯ в 1С.

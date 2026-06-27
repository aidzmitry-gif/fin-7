# Модуль finance — контекст для Claude

**Тип:** git submodule → fin-7 (правка = коммит в этот репозиторий)
**API-префикс:** `/finance`
**Схема БД:** `finance`
**Статус:** заглушка-каркас (одна таблица `payment`, минимальный CRUD)

## Назначение
Учёт платежей: фиксация платежа по ссылке (`ref`) на сделку/документ, сумма, статус
(`pending`/`paid`). Замыкает цепочку «счёт из sales → платёж в finance → отметка об оплате
обратно в sales» через шину событий.

## Файлы
- `module.py` — `FinanceModule(ModuleContract)`, фабрика `get_module()`; `name="finance"`, `version="0.1.0"`, `api_prefix="/finance"`.
- `models.py` — ORM `Payment` (схема `finance`).
- `routes.py` — HTTP-API платежей (router без своего префикса, монтируется под `/finance`).
- `events.py` — обработчики проводок: `on_document_posted`, `on_freight_cost`, `on_freight_refund`, `on_landed_cost`.
- `summary.py` — `finance_summary(session)`: агрегат маржи (по фактам) и кассы (ДДС-lite) по `kind`.
- `schemas.py` — Pydantic: `PaymentCreate`, `PaymentOut`, `StatusUpdate`.
- `__init__.py` — пакет-маркер.

## Что регистрирует в ядре (register())
- **Роуты**: `core.include_router(routes.router, prefix="/finance")`.
- **Подписки**: `core.subscribe("sales.document.posted", on_document_posted)`;
  `core.subscribe("logistics.freight.cost", on_freight_cost)` (расход на фрахт из логистики);
  `core.subscribe("logistics.freight.audit_refund", on_freight_refund)` (переплата к возврату → кредит против фрахта);
  `core.subscribe("procurement.landed_cost.calculated", on_landed_cost)` (себестоимость прихода → расход; ⚠ закупки пока НЕ эмитят — потребитель готов заранее, Горизонт 2).
- **Widgets**: `Widget("finance", "Финансы", source="finance.payments")`.
- Workflow / permissions / roles / telegram / startup — не регистрирует.

## События
- **Публикует**:
  - `finance.payment.created` — из `on_document_posted` при создании платежа из счёта; payload `{ref, amount, deal_id, entity_ref}`.
  - `finance.payment.paid` — из `PATCH /payments/{id}` при статусе `paid`; payload `{ref, entity_ref: "payment:<id>"}`.
- **Подписан на**:
  - `sales.document.posted` → `on_document_posted` (реагирует только при `payload.kind == "invoice"`).
  - `logistics.freight.cost` → `on_freight_cost` (доставка завершена → `Payment(kind="freight")`, расход; нулевой тариф игнорится).
  - `logistics.freight.audit_refund` → `on_freight_refund` (аудит счёта, `variance > 0` → `Payment(kind="freight_refund")` с **отрицательной** суммой — кредит против фрахта; нулевая сумма игнорится). payload `{shipment_code, carrier, amount, entity_ref:"audit:<id>"}`.
  - `procurement.landed_cost.calculated` → `on_landed_cost` (себестоимость прихода → `Payment(kind="landed")`, расход; сумма = `amount` либо `unit_landed_cost_byn × qty`; нуль игнорится). ⚠ **Закупки пока это событие НЕ эмитят** (Горизонт 2) — проводки `landed` появятся, когда закупки начнут эмитить; до тех пор honest-empty.

## Модель данных (таблицы схемы)
- `finance.payment` (`Payment`): `id` (PK), `ref` (str 255), `amount` (Numeric(14,2), default 0),
  `status` (str 32, default `pending`), `kind` (str 32, default `receivable`; `receivable` — доход/счёт
  к получению, `freight` — расход на перевозку, `freight_refund` — возврат переплаты перевозчиком,
  отрицательная сумма = кредит против фрахта), `created_at` (DateTime, server_default `now()`).
  FK/связей нет — связь со сделкой логическая, через `ref` (фрахт `ref = "freight:<№отгрузки>"`,
  возврат `ref = "freight_refund:audit:<id>"`).
  Колонка `kind` добавлена миграцией 0053.

## API-эндпоинты (ключевые)
- `GET /finance/summary` — операционная сводка (`summary.py::finance_summary`): фактическая маржа
  (выручка − landed − чистый фрахт), касса ДДС-lite (приток/отток/сальдо, поступило/к поступлению),
  затраты по типам. Все суммы — BYN. Пустая база → нули + `margin.pct=None` (honest-empty).
- `GET /finance/payments` — список платежей (сорт. по `id` desc).
- `POST /finance/payments` — зафиксировать платёж (201).
- `PATCH /finance/payments/{payment_id}` — сменить статус; при `paid` эмитит `finance.payment.paid` (404, если платёж не найден).

## Межмодульные связи и зависимости
- **sales → finance**: счёт (`sales.document.posted`, `kind="invoice"`) создаёт `Payment(status="pending")`.
- **logistics → finance**: доставка завершена (`logistics.freight.cost`) → `Payment(kind="freight")` (расход на фрахт; основа для подсчёта валовой прибыли: доход − фрахт).
- **finance → sales**: при оплате `finance.payment.paid` ловит sales (`on_payment_paid`) → помечает документ оплаченным.
- На `sales.document.posted` подписан также logistics — finance не единственный потребитель.
- Прямых импортов sales нет: взаимодействие только через шину/`core` (§2.4–2.5).

## Подводные камни / детали
- `POST /payments` сам коммитит сессию (`session.commit()`) — отступление от паттерна «роут владеет транзакцией без двойного коммита»; учитывать при изменениях.
- `on_document_posted` пишет через `ctx.session` и эмитит через `ctx.services.event_bus` — коммит делает доставщик outbox, не обработчик.
- `amount` в API — `float` (схемы), в ORM — `Decimal`; в роуте конвертация `Decimal(str(...))`.
- Статусы — свободные строки (нет enum); особый только `paid` (триггерит событие).
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
- `events.py` — обработчик `on_document_posted(payload, ctx)`.
- `schemas.py` — Pydantic: `PaymentCreate`, `PaymentOut`, `StatusUpdate`.
- `__init__.py` — пакет-маркер.

## Что регистрирует в ядре (register())
- **Роуты**: `core.include_router(routes.router, prefix="/finance")`.
- **Подписки**: `core.subscribe("sales.document.posted", on_document_posted)`;
  `core.subscribe("logistics.freight.cost", on_freight_cost)` (расход на фрахт из логистики).
- **Widgets**: `Widget("finance", "Финансы", source="finance.payments")`.
- Workflow / permissions / roles / telegram / startup — не регистрирует.

## События
- **Публикует**:
  - `finance.payment.created` — из `on_document_posted` при создании платежа из счёта; payload `{ref, amount, deal_id, entity_ref}`.
  - `finance.payment.paid` — из `PATCH /payments/{id}` при статусе `paid`; payload `{ref, entity_ref: "payment:<id>"}`.
- **Подписан на**:
  - `sales.document.posted` → `on_document_posted` (реагирует только при `payload.kind == "invoice"`).
  - `logistics.freight.cost` → `on_freight_cost` (доставка завершена → `Payment(kind="freight")`, расход; нулевой тариф игнорится).

## Модель данных (таблицы схемы)
- `finance.payment` (`Payment`): `id` (PK), `ref` (str 255), `amount` (Numeric(14,2), default 0),
  `status` (str 32, default `pending`), `kind` (str 32, default `receivable`; `receivable` — доход/счёт
  к получению, `freight` — расход на перевозку), `created_at` (DateTime, server_default `now()`).
  FK/связей нет — связь со сделкой логическая, через `ref` (для фрахта `ref = "freight:<№отгрузки>"`).
  Колонка `kind` добавлена миграцией 0053.

## API-эндпоинты (ключевые)
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
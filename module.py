"""Модуль Finance (Финансы) — реализация ModuleContract."""
from __future__ import annotations

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.finance import routes
from modules.finance.document_versions import on_original_issued, on_original_superseded
from modules.finance.events import (
    on_claim_resolved,
    on_deal_handoff,
    on_freight_cost,
    on_freight_refund,
    on_landed_cost,
    on_payroll_accrued,
    on_payroll_paid,
    on_po_drafted,
    on_reference_changed,
)


class FinanceModule(ModuleContract):
    name = "finance"
    version = "0.1.0"
    api_prefix = "/finance"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        # межмодульная связь: счёт из sales → платёж в finance (§2.5)
        core.subscribe("sales.document.posted", on_original_issued)
        core.subscribe("sales.document.superseded", on_original_superseded)
        # логистика → финансы: доставлено → расход на фрахт (§2.5)
        core.subscribe("logistics.freight.cost", on_freight_cost)
        # логистика → финансы: аудит счёта выявил переплату → возврат (кредит против фрахта)
        core.subscribe("logistics.freight.audit_refund", on_freight_refund)
        # закупки → финансы: себестоимость прихода → проводка-затрата (для фактической маржи)
        core.subscribe("procurement.landed_cost.calculated", on_landed_cost)
        # закупки → финансы (FIN-C1): компенсация по претензии (resolved+amount_byn>0)
        core.subscribe("procurement.claim.resolved", on_claim_resolved)
        # закупки → финансы (FIN-B1): PO выписан → планируемый отток в cashflow-прогноз
        core.subscribe("procurement.po.drafted", on_po_drafted)
        # справочники → финансы (B2 Круг 4): смена ставки/мастер-полей SKU → recompute-сигнал
        # downstream Закупкам (outbox-паттерн; finance остаётся единым писателем проводок).
        core.subscribe("reference.sku.changed", on_reference_changed)
        core.subscribe("reference.ref_tnved.changed", on_reference_changed)
        core.subscribe("reference.ref_vat_rate.changed", on_reference_changed)
        core.subscribe("reference.ref_currency_rate.changed", on_reference_changed)
        # HR → финансы (Р5): ФОТ начислен / выплачен
        core.subscribe("hr.payroll.accrued", on_payroll_accrued)
        core.subscribe("hr.payroll.paid", on_payroll_paid)
        # sales → финансы (Р5): сделка отгружена → признание выручки (accrual по отгрузке)
        core.subscribe("sales.deal.handoff", on_deal_handoff)
        core.register_widget(Widget("finance", "Финансы", source="finance.payments"))


def get_module() -> ModuleContract:
    return FinanceModule()

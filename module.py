"""Модуль Finance (Финансы) — реализация ModuleContract."""
from __future__ import annotations

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.finance import routes
from modules.finance.events import (
    on_document_posted,
    on_freight_cost,
    on_freight_refund,
    on_landed_cost,
)


class FinanceModule(ModuleContract):
    name = "finance"
    version = "0.1.0"
    api_prefix = "/finance"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        # межмодульная связь: счёт из sales → платёж в finance (§2.5)
        core.subscribe("sales.document.posted", on_document_posted)
        # логистика → финансы: доставлено → расход на фрахт (§2.5)
        core.subscribe("logistics.freight.cost", on_freight_cost)
        # логистика → финансы: аудит счёта выявил переплату → возврат (кредит против фрахта)
        core.subscribe("logistics.freight.audit_refund", on_freight_refund)
        # закупки → финансы: себестоимость прихода → проводка-затрата (для фактической маржи)
        core.subscribe("procurement.landed_cost.calculated", on_landed_cost)
        core.register_widget(Widget("finance", "Финансы", source="finance.payments"))


def get_module() -> ModuleContract:
    return FinanceModule()

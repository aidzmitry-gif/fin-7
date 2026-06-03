"""Модуль Finance (Финансы) — реализация ModuleContract."""
from __future__ import annotations

from core.runtime.contract import ModuleContract, Widget
from core.runtime.core import Core
from modules.finance import routes


class FinanceModule(ModuleContract):
    name = "finance"
    version = "0.1.0"
    api_prefix = "/finance"

    def register(self, core: Core) -> None:
        core.include_router(routes.router, prefix=self.api_prefix)
        core.register_widget(Widget("finance", "Финансы", source="finance.payments"))


def get_module() -> ModuleContract:
    return FinanceModule()

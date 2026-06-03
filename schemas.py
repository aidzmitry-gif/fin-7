"""Pydantic-схемы модуля Finance."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PaymentCreate(BaseModel):
    ref: str
    amount: float = 0
    status: str = "pending"


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ref: str
    amount: float
    status: str

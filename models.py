"""ORM-модели модуля Finance (схема ``finance.*``)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db.base import Base


class Payment(Base):
    """Платёж: назначение (сделка/документ), сумма, статус."""

    __tablename__ = "payment"
    __table_args__ = {"schema": "finance"}

    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

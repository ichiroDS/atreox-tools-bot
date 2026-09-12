from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# A creator brands with a handful of handles, not a catalogue.
MAX_PRESETS_PER_USER = 10


class WatermarkPreset(Base):
    """A saved watermark: its text and the look the user picked for it."""

    __tablename__ = "watermark_presets"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", "name", name="uq_watermark_presets_user_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(String(64))
    position: Mapped[str] = mapped_column(String(16))
    style: Mapped[str] = mapped_column(String(16))
    size: Mapped[str] = mapped_column(String(8))
    opacity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<WatermarkPreset {self.id} {self.name!r}>"

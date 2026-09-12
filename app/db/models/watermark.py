from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# A creator brands with a handful of handles, not a catalogue.
MAX_PRESETS_PER_USER = 10

# Logo presets are stored in the database, because Railway's filesystem is
# ephemeral - a redeploy would otherwise lose every saved logo. A brand mark is
# a few tens of kilobytes; this is the ceiling we accept.
MAX_LOGO_BYTES = 1024 * 1024


class WatermarkPreset(Base):
    """A saved watermark: its text and the look the user picked for it."""

    __tablename__ = "watermark_presets"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", "name", name="uq_watermark_presets_user_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # "text" or "logo": a logo preset carries its image instead of a line.
    kind: Mapped[str] = mapped_column(String(8), default="text", server_default="text")
    text: Mapped[str] = mapped_column(String(64))
    logo: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    logo_format: Mapped[str | None] = mapped_column(String(8), nullable=True)
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

    @property
    def is_logo(self) -> bool:
        return self.kind == "logo" and bool(self.logo)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<WatermarkPreset {self.id} {self.kind} {self.name!r}>"

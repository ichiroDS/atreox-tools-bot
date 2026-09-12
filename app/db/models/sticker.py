from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class StickerSet(Base):
    """One curated sticker pack in our own discovery catalog."""

    __tablename__ = "sticker_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_set_name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(64), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    samples: Mapped[list["StickerSample"]] = relationship(
        back_populates="sticker_set",
        cascade="all, delete-orphan",
        lazy="selectin",
        # Pack order, and explicitly so: a database is free to return rows in
        # any order without it, and which sticker represents a pack is chosen
        # by position.
        order_by="StickerSample.id",
    )


class StickerSample(Base):
    """A single sticker we may show as a representative of its pack."""

    __tablename__ = "sticker_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sticker_set_id: Mapped[int] = mapped_column(
        ForeignKey("sticker_sets.id", ondelete="CASCADE"), index=True
    )
    telegram_file_id: Mapped[str] = mapped_column(String(255))
    emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    weight: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    sticker_set: Mapped[StickerSet] = relationship(back_populates="samples")

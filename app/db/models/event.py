from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Feature(str, enum.Enum):
    CIRCLE = "circle"
    VOICE_NOTE = "voice_note"
    MEDIA_OPTIMIZE = "media_optimize"
    WATERMARK = "watermark"
    # The watermark is a picture the user uploaded, not a line of text.
    WATERMARK_LOGO = "watermark_logo"
    CONVERT = "convert"
    MAKE_STICKER = "make_sticker"
    EXTRACT_FRAME = "extract_frame"
    # One batch: started when processing begins, completed when it ends.
    BATCH_STARTED = "batch_started"
    BATCH_COMPLETED = "batch_completed"
    # Which Media Optimizer preset was picked.
    OPTIMIZE_SMALL = "optimize_small"
    OPTIMIZE_BALANCED = "optimize_balanced"
    OPTIMIZE_HIGH = "optimize_high"
    METADATA = "metadata"
    STICKERS = "stickers"
    HELP = "help"
    PRIVACY = "privacy"
    # Recorded when a user taps through the AtreoxAI call to action.
    CTA_CLICK = "cta_click"


class FeatureEvent(Base):
    """Lightweight funnel analytics: which tool a user picked, and when."""

    __tablename__ = "feature_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    feature: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

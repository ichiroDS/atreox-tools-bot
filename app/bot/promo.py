"""Delivering the AtreoxAI call to action, sparingly.

One place decides whether the promo appears, so no flow can accidentally start
showing it more often than the others.
"""

from __future__ import annotations

import logging
from typing import Any

from app.bot import texts
from app.bot.keyboards.common import cta_keyboard
from app.db.models import User
from app.services.cta import record_cta_shown, should_show_cta

logger = logging.getLogger(__name__)


async def maybe_send_cta(message: Any, user: User | None) -> bool:
    """Send the promo if this user is due one. Returns whether it was sent.

    Called after the result has already been delivered, so a failure here can
    never cost the user their file.
    """
    if not should_show_cta(user):
        return False

    try:
        await message.answer(texts.CTA, reply_markup=cta_keyboard())
    except Exception:  # noqa: BLE001 - the promo is never worth an error
        logger.warning("failed to deliver the CTA")
        return False

    record_cta_shown(user)
    logger.info(
        "cta shown telegram_user_id=%s count=%s",
        user.telegram_user_id,
        user.cta_shown_count,
    )
    return True

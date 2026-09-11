"""Typed callback-data factories.

Keeping them in one module makes the callback namespace easy to audit; every
payload stays well inside Telegram's 64-byte budget.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MenuCallback(CallbackData, prefix="menu"):
    action: str  # circle | metadata | stickers | help | main


class CircleCallback(CallbackData, prefix="crc"):
    # forward_help | guide_ios | guide_android | forward_back
    # split | first | cancel   (the long-video choice)
    action: str


class MetadataCallback(CallbackData, prefix="meta"):
    action: str  # clean | change | device | tod | apply | restart | cancel
    value: str = ""


class StickerCallback(CallbackData, prefix="stk"):
    action: str  # category | more | categories
    category: str = ""

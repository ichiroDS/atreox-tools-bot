"""Typed callback-data factories.

Keeping them in one module makes the callback namespace easy to audit; every
payload stays well inside Telegram's 64-byte budget.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MenuCallback(CallbackData, prefix="menu"):
    action: str  # circle | voice | optimize | metadata | stickers | help | main


class CircleCallback(CallbackData, prefix="crc"):
    # forward_help | guide_ios | guide_android | forward_back
    # split | first | cancel   (the long-video choice)
    action: str


class VoiceCallback(CallbackData, prefix="vn"):
    action: str  # forward_help | guide_ios | guide_android | forward_back


class OptimizeCallback(CallbackData, prefix="opt"):
    action: str  # preset | cancel
    value: str = ""  # small | balanced | high


class BatchCallback(CallbackData, prefix="bt"):
    # done | tool | preset | new | pos | style | size | opacity | cancel
    action: str
    value: str = ""  # a tool name, an enum value, or a preset id


class WatermarkCallback(CallbackData, prefix="wm"):
    # enter | presets | pos | style | size | opacity | apply | save | change
    # detail | use | rename | edit | delete | delete_yes | new | back | cancel
    action: str
    value: str = ""  # an enum value, or a preset id


class MetadataCallback(CallbackData, prefix="meta"):
    action: str  # clean | change | device | tod | apply | restart | cancel
    value: str = ""


class StickerCallback(CallbackData, prefix="stk"):
    action: str  # category | more | categories
    category: str = ""

"""Typed callback-data factories.

Keeping them in one module makes the callback namespace easy to audit; every
payload stays well inside Telegram's 64-byte budget.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class MenuCallback(CallbackData, prefix="menu"):
    # circle | voice | optimize | metadata | watermark | batch | animation
    # frame | make_sticker | stickers | help | privacy | cta | main
    action: str


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
    # type | enter | logo | presets | pos | style | size | opacity | apply
    # save | change | detail | use | rename | edit | delete | delete_yes | new
    # back | cancel
    action: str
    value: str = ""  # an enum value, "text"/"logo", or a preset id


class AnimationCallback(CallbackData, prefix="anm"):
    action: str  # convert | clip | cancel
    value: str = ""  # to_gif | to_mp4 | optimize_gif, or first | middle | custom


class FrameCallback(CallbackData, prefix="frm"):
    action: str  # pos | format | cancel
    value: str = ""  # a position, or jpeg | png


class MakeStickerCallback(CallbackData, prefix="mks"):
    action: str  # style | cancel
    value: str = ""  # clean | white_outline | black_outline


class MetadataCallback(CallbackData, prefix="meta"):
    action: str  # clean | change | device | tod | apply | restart | cancel
    value: str = ""


class StickerCallback(CallbackData, prefix="stk"):
    action: str  # category | more | categories
    category: str = ""

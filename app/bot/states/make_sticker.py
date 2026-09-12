from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class MakeStickerStates(StatesGroup):
    waiting_for_image = State()
    choosing_style = State()

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class FrameStates(StatesGroup):
    waiting_for_media = State()
    choosing_position = State()
    typing_time = State()
    choosing_format = State()

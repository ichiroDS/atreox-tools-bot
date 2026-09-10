from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CircleStates(StatesGroup):
    waiting_for_video = State()

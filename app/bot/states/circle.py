from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CircleStates(StatesGroup):
    waiting_for_video = State()
    # A video longer than one circle arrived; waiting for split / first / cancel.
    choosing_long_mode = State()

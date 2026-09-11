from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class OptimizerStates(StatesGroup):
    waiting_for_media = State()
    # The file has been analysed; waiting for Small / Balanced / High / Cancel.
    choosing_preset = State()

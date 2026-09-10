from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class CleanMetadataStates(StatesGroup):
    waiting_for_file = State()


class ChangeMetadataStates(StatesGroup):
    """Device and location are each asked in two steps, narrow then specific."""

    waiting_for_file = State()
    choosing_generation = State()
    choosing_model = State()
    choosing_country = State()
    choosing_city = State()
    choosing_time_of_day = State()
    confirming = State()

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class WatermarkStates(StatesGroup):
    waiting_for_media = State()
    # The file is in hand: text, a logo, or something already saved.
    choosing_type = State()
    # The text can be typed or taken from a preset.
    choosing_source = State()
    typing_text = State()
    waiting_for_logo = State()
    choosing_position = State()
    choosing_style = State()
    choosing_size = State()
    choosing_opacity = State()
    confirming = State()
    # Preset management, which is also reachable without a file in flight.
    typing_new_preset = State()
    renaming_preset = State()
    retyping_preset_text = State()

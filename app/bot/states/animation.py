from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AnimationStates(StatesGroup):
    waiting_for_media = State()
    # The file has been probed; waiting for GIF / MP4 / Optimize / Cancel.
    choosing_conversion = State()
    # A long video: which six seconds of it should become the GIF.
    choosing_clip = State()
    typing_start_time = State()

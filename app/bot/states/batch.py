from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class BatchStates(StatesGroup):
    # Files are arriving - one by one, or as album items.
    collecting = State()
    choosing_tool = State()
    # Watermark: a saved preset, or a new one configured once for the batch.
    choosing_watermark = State()
    typing_watermark_text = State()
    choosing_position = State()
    choosing_style = State()
    choosing_size = State()
    choosing_opacity = State()
    choosing_optimizer_preset = State()
    # The batch is running; new files have to wait for the next one.
    processing = State()

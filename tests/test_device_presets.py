from __future__ import annotations

import dataclasses

import pytest

from app.data.device_presets import DEVICE_PRESETS, DevicePreset, get_preset, validate_presets


def test_shipped_catalog_is_valid():
    validate_presets()


def test_every_preset_is_an_apple_device_with_a_model():
    for preset in DEVICE_PRESETS:
        assert preset.make == "Apple"
        assert preset.model


def test_get_preset_round_trip():
    for preset in DEVICE_PRESETS:
        assert get_preset(preset.key) is preset


def test_get_preset_unknown_key():
    assert get_preset("nokia_3310") is None


def test_rejects_empty_catalog():
    with pytest.raises(ValueError):
        validate_presets(())


def test_rejects_duplicate_keys():
    preset = DEVICE_PRESETS[0]
    with pytest.raises(ValueError, match="duplicate"):
        validate_presets((preset, preset))


def test_rejects_key_too_long_for_callback_data():
    bad = dataclasses.replace(DEVICE_PRESETS[0], key="x" * 40)
    with pytest.raises(ValueError, match="too long"):
        validate_presets((bad,))


def test_rejects_missing_model():
    bad = DevicePreset(key="ghost", label="Ghost", generation=14, variant="",
                        make="Apple", model="")
    with pytest.raises(ValueError, match="make and model"):
        validate_presets((bad,))


def test_rejects_empty_label():
    bad = DevicePreset(key="ghost", label="  ", generation=14, variant="",
                        make="Apple", model="iPhone")
    with pytest.raises(ValueError, match="empty label"):
        validate_presets((bad,))

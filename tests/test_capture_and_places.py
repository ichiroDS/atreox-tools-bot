"""The catalogs and the exposure model behind believable metadata."""

from __future__ import annotations

import random

import pytest

from app.data.device_presets import (
    DEVICE_PRESETS,
    GENERATIONS,
    generation_label,
    get_preset,
    presets_for_generation,
)
from app.data.locations import COUNTRIES, get_city, get_country, validate_catalog
from app.services.media.capture import derive_shot
from app.services.stickers.catalog import CATEGORY_KEYS
from app.services.stickers.packs import CURATED_PACKS, packs_for_category, validate_packs
from app.services.timeofday import TimeOfDay

ALL_PRESETS = pytest.mark.parametrize("preset", DEVICE_PRESETS, ids=lambda p: p.key)


# --- devices ----------------------------------------------------------------


def test_every_generation_from_x_to_16_is_offered():
    assert set(GENERATIONS) == {10, 11, 12, 13, 14, 15, 16}


def test_generation_ten_is_written_as_x():
    assert generation_label(10) == "X"
    # Apple brands these as whole names, so the model string must read
    # "iPhone XS Max" rather than "iPhone X S Max".
    assert get_preset("iphone_x").model == "iPhone X"
    assert get_preset("iphone_xr").model == "iPhone XR"
    assert get_preset("iphone_xs").model == "iPhone XS"
    assert get_preset("iphone_xs_max").model == "iPhone XS Max"


def test_every_generation_offers_a_pro_max_and_a_standard():
    for generation in GENERATIONS:
        variants = {p.variant for p in presets_for_generation(generation)}
        assert "" in variants, f"iPhone {generation} has no standard model"
        # The X generation calls its big model "S Max" rather than "Pro Max".
        assert {"Pro Max", "S Max"} & variants, f"iPhone {generation} has no max model"


def test_variant_buttons_read_as_real_product_names():
    labels = {p.key: p.variant_label for p in DEVICE_PRESETS}
    assert labels["iphone_xs_max"] == "XS Max"
    assert labels["iphone_xr"] == "XR"
    assert labels["iphone_16_pro_max"] == "Pro Max"
    assert labels["iphone_16"] == "Standard"


@ALL_PRESETS
def test_presets_carry_consistent_camera_figures(preset):
    assert preset.make == "Apple"
    assert preset.model == preset.label
    assert preset.f_number > 0
    assert preset.focal_length > 0
    assert preset.focal_length_35mm in (24, 26)
    # The lens description must agree with the numbers written beside it.
    assert f"{preset.focal_length}mm" in preset.lens_model
    assert f"f/{preset.f_number}" in preset.lens_model
    assert preset.model in preset.lens_model
    # LensInfo is "min focal, max focal, min aperture, max aperture".
    assert len(preset.lens_info.split()) == 4


# --- places -----------------------------------------------------------------


def test_the_requested_countries_are_all_present():
    assert {c.key for c in COUNTRIES} == {"usa", "spain", "italy", "england", "japan"}


def test_every_country_offers_five_cities():
    for country in COUNTRIES:
        assert len(country.cities) == 5, country.key


def test_catalog_validates():
    validate_catalog()


def test_city_lookup_returns_its_country():
    found = get_city("miami")
    assert found is not None
    country, city = found
    assert country.key == "usa"
    assert city.label == "Miami"
    assert get_city("atlantis") is None
    assert get_country("narnia") is None


def test_coordinates_are_jittered_but_stay_local():
    _, city = get_city("tokyo")
    seen = set()
    for seed in range(20):
        lat, lon = city.jittered(random.Random(seed))
        seen.add((lat, lon))
        # Within roughly two kilometres of the city centre.
        assert abs(lat - city.latitude) <= 0.02
        assert abs(lon - city.longitude) <= 0.02
    # Every photo landing on the identical point is the tell we avoid.
    assert len(seen) > 1


def test_timezones_are_plausible_for_their_country():
    expected = {"usa": {-5, -6, -8}, "spain": {1}, "italy": {1}, "england": {0}, "japan": {9}}
    for country in COUNTRIES:
        for city in country.cities:
            assert city.utc_offset in expected[country.key], city.key


# --- exposure ---------------------------------------------------------------


@pytest.mark.parametrize("seed", range(5))
def test_night_shots_are_slower_and_noisier_than_daylight(seed):
    preset = get_preset("iphone_16_pro")
    rng = random.Random(seed)
    day = derive_shot(preset, TimeOfDay.DAY, width=4032, height=3024, rng=rng)
    night = derive_shot(preset, TimeOfDay.NIGHT, width=4032, height=3024, rng=rng)

    assert night.iso > day.iso
    assert night.exposure_time > day.exposure_time
    assert night.brightness_value < day.brightness_value


@pytest.mark.parametrize("time_of_day", list(TimeOfDay))
@pytest.mark.parametrize("seed", range(10))
def test_shot_figures_stay_inside_camera_limits(time_of_day, seed):
    preset = get_preset("iphone_13_pro")
    shot = derive_shot(
        preset, time_of_day, width=4032, height=3024, rng=random.Random(seed)
    )
    assert preset.base_iso <= shot.iso <= 6400
    # No phone hands out a shutter slower than 1/15 or faster than 1/8000.
    assert 1 / 8000 <= shot.exposure_time <= 1 / 15
    assert shot.aperture_value == preset.f_number
    assert 0 <= shot.gps_direction <= 360
    assert 0 < shot.gps_h_positioning_error < 15
    assert 0 <= shot.subsec < 1000


def test_subject_area_is_four_numbers_scaled_to_the_frame():
    shot = derive_shot(
        get_preset("iphone_12"),
        TimeOfDay.DAY,
        width=2160,
        height=2880,
        rng=random.Random(1),
    )
    parts = [int(p) for p in shot.subject_area.split()]
    assert len(parts) == 4
    centre_x, centre_y, area_w, area_h = parts
    assert 0 < centre_x < 2160
    assert 0 < centre_y < 2880
    assert 0 < area_w <= 2160
    assert 0 < area_h <= 2880


def test_a_seeded_shot_is_reproducible():
    args = dict(width=4032, height=3024, altitude=10.0)
    first = derive_shot(get_preset("iphone_15"), TimeOfDay.EVENING, rng=random.Random(4), **args)
    second = derive_shot(get_preset("iphone_15"), TimeOfDay.EVENING, rng=random.Random(4), **args)
    assert first == second


def test_altitude_follows_the_chosen_city():
    _, city = get_city("las_vegas")  # 610 m
    shot = derive_shot(
        get_preset("iphone_14"),
        TimeOfDay.DAY,
        width=100,
        height=100,
        altitude=city.altitude,
        rng=random.Random(0),
    )
    assert abs(shot.gps_altitude - city.altitude) < 10


# --- sticker packs ----------------------------------------------------------


def test_curated_packs_validate():
    validate_packs()


def test_every_sticker_category_has_curated_packs():
    for category in CATEGORY_KEYS:
        assert packs_for_category(category), f"{category} has no packs"


def test_curated_pack_names_are_unique():
    names = [p.telegram_set_name for p in CURATED_PACKS]
    assert len(names) == len(set(names))

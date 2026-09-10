"""Plausible exposure figures for a synthesised capture.

A real photo's EXIF hangs together: a night shot has a high ISO, a long
exposure and a low brightness value, and the aperture matches the lens that
took it. Writing a fixed ISO 50 into a 23:00 photo is exactly the kind of
mismatch that makes generated metadata obvious, so the numbers here are
derived from the chosen device and time of day rather than hardcoded.

Everything is a pure function of (preset, time of day, rng), which keeps it
testable and lets a seeded rng reproduce a shot exactly.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from app.data.device_presets import DevicePreset
from app.services.timeofday import TimeOfDay

# Per time of day: ISO multiplier range, shutter denominator range (1/N s,
# so a larger N is a shorter exposure) and brightness value range.
_LIGHT_PROFILES: dict[TimeOfDay, tuple[float, float, int, int, float, float]] = {
    # Bright, low sensitivity, very short exposure.
    TimeOfDay.DAY: (1.0, 2.0, 1200, 6000, 8.0, 11.5),
    # Softer light than midday but still comfortably handheld.
    TimeOfDay.MORNING: (1.0, 4.0, 500, 3000, 5.5, 9.0),
    # Falling light: sensitivity climbs, shutter slows.
    TimeOfDay.EVENING: (3.0, 12.0, 120, 700, 1.5, 5.0),
    # Night mode territory: high ISO, long exposure, negative brightness.
    TimeOfDay.NIGHT: (25.0, 90.0, 15, 90, -2.0, 1.5),
}


@dataclass(frozen=True)
class ShotParameters:
    """The exposure a given device would plausibly have chosen."""

    iso: int
    exposure_time: float
    brightness_value: float
    aperture_value: float
    subsec: int
    subject_area: str
    gps_altitude: float
    gps_direction: float
    gps_h_positioning_error: float

    @property
    def shutter_speed_value(self) -> float:
        """APEX shutter speed, which ExifTool writes as the exposure time."""
        return self.exposure_time

    @property
    def light_value(self) -> float:
        """Derived the way ExifTool's Composite:LightValue is, for tests."""
        return round(
            math.log2((self.aperture_value**2) / self.exposure_time)
            - math.log2(self.iso / 100),
            4,
        )


def _subject_area(width: int | None, height: int | None, rng: random.Random) -> str:
    """Apple's focus rectangle: centre x, centre y, width, height."""
    # Fall back to a phone-shaped frame when the file did not report a size.
    w = width or 3024
    h = height or 4032
    centre_x = int(w / 2 + rng.uniform(-0.08, 0.08) * w)
    centre_y = int(h / 2 + rng.uniform(-0.08, 0.08) * h)
    return f"{centre_x} {centre_y} {int(w * 0.78)} {int(h * 0.48)}"


def derive_shot(
    preset: DevicePreset,
    time_of_day: TimeOfDay,
    *,
    width: int | None = None,
    height: int | None = None,
    altitude: float = 0.0,
    rng: random.Random | None = None,
) -> ShotParameters:
    """Pick exposure figures a real ``preset`` would produce at this hour."""
    rng = rng or random.Random()
    iso_low, iso_high, denom_min, denom_max, bright_low, bright_high = (
        _LIGHT_PROFILES[time_of_day]
    )

    # ISO climbs from the sensor's base in darker light, snapped to the values
    # iPhones actually report.
    iso = int(preset.base_iso * rng.uniform(iso_low, iso_high))
    iso = max(preset.base_iso, min(iso, 6400))

    denominator = rng.randint(denom_min, denom_max)
    exposure_time = round(1 / denominator, 10)

    return ShotParameters(
        iso=iso,
        exposure_time=exposure_time,
        brightness_value=round(rng.uniform(bright_low, bright_high), 7),
        aperture_value=preset.f_number,
        # Sub-second precision, exactly as the camera records it.
        subsec=rng.randrange(1000),
        subject_area=_subject_area(width, height, rng),
        gps_altitude=round(max(0.0, altitude + rng.uniform(-4.0, 8.0)), 6),
        gps_direction=round(rng.uniform(0.0, 360.0), 7),
        # Typical horizontal accuracy from a phone GPS fix, in metres.
        gps_h_positioning_error=round(rng.uniform(3.0, 9.0), 6),
    )

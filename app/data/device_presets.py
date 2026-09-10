"""Device presets used by the "Change Metadata" wizard.

This is the single place where devices are defined - handlers and keyboards
build themselves from ``DEVICE_PRESETS`` so adding a model never means touching
bot code.

The wizard asks in two steps, generation then variant, which is why every
preset carries both. Camera figures (aperture, focal length, lens description)
are the real published values for each model, so the metadata we write hangs
together the way a genuine capture would.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DevicePreset:
    """A camera identity that can be written into image/video metadata."""

    key: str
    # Plain model name. The 📱 prefix is added by the keyboard, so the label can
    # also be dropped straight into the confirmation summary.
    label: str
    # iPhone generation (10-16) and the variant within it ("", "Pro", ...).
    generation: int
    variant: str
    make: str
    model: str
    software: str | None = None
    lens_model: str | None = None
    # --- main (wide) camera figures, as published by Apple ------------------
    f_number: float = 1.8
    focal_length: float = 4.25
    focal_length_35mm: int = 26
    # EXIF LensInfo: min focal, max focal, min aperture, max aperture across
    # the whole camera system.
    lens_info: str = "1.54 4.25 1.8 2.4"
    # Base sensitivity in good light; the shot profile scales up from here.
    base_iso: int = 32
    # Extra EXIF tags applied to still images (ExifTool tag name -> value).
    exif_extra: dict[str, str] = field(default_factory=dict)
    # Extra QuickTime tags applied to MP4/MOV containers.
    quicktime_extra: dict[str, str] = field(default_factory=dict)

    @property
    def generation_label(self) -> str:
        """How the generation is written on the device, ``X`` for 10."""
        return generation_label(self.generation)

    @property
    def variant_label(self) -> str:
        """Button text for the variant step.

        The X generation brands its variants as whole names (XR, XS Max), so
        showing a bare "R" there would be meaningless.
        """
        if self.generation == 10:
            return self.model.removeprefix("iPhone ")
        return self.variant or "Standard"


def generation_label(generation: int) -> str:
    return "X" if generation == 10 else str(generation)


def _preset(
    generation: int,
    variant: str,
    *,
    software: str,
    lens: str,
    f_number: float,
    focal_length: float,
    lens_info: str,
    focal_length_35mm: int = 26,
    base_iso: int = 32,
) -> DevicePreset:
    """Build one preset, deriving key/label/model from generation + variant.

    The X generation joins its variant without a space - "XS Max", never
    "X S Max" - which is exactly how the model name appears in real EXIF.
    """
    separator = "" if generation == 10 else " "
    suffix = f"{separator}{variant}" if variant else ""
    model = f"iPhone {generation_label(generation)}{suffix}"
    key = "iphone_" + model.removeprefix("iPhone ").lower().replace(" ", "_")
    return DevicePreset(
        key=key,
        label=model,
        generation=generation,
        variant=variant,
        make="Apple",
        model=model,
        software=software,
        lens_model=f"{model} back {lens} {focal_length}mm f/{f_number}",
        f_number=f_number,
        focal_length=focal_length,
        focal_length_35mm=focal_length_35mm,
        lens_info=lens_info,
        base_iso=base_iso,
    )


# Ordered oldest to newest; the keyboard groups them by generation.
DEVICE_PRESETS: tuple[DevicePreset, ...] = (
    # --- iPhone X (10) ------------------------------------------------------
    _preset(10, "", software="16.7.10", lens="dual camera", f_number=1.8,
            focal_length=4.0, lens_info="4.0 6.0 1.8 2.4", base_iso=25),
    _preset(10, "R", software="16.7.10", lens="camera", f_number=1.8,
            focal_length=4.25, lens_info="4.25 4.25 1.8 1.8", base_iso=25),
    _preset(10, "S", software="16.7.10", lens="dual camera", f_number=1.8,
            focal_length=4.25, lens_info="4.25 6.0 1.8 2.4", base_iso=25),
    _preset(10, "S Max", software="16.7.10", lens="dual camera", f_number=1.8,
            focal_length=4.25, lens_info="4.25 6.0 1.8 2.4", base_iso=25),
    # --- iPhone 11 ----------------------------------------------------------
    _preset(11, "", software="17.6.1", lens="dual wide camera", f_number=1.8,
            focal_length=4.25, lens_info="1.54 4.25 1.8 2.4"),
    _preset(11, "Pro", software="17.6.1", lens="triple camera", f_number=1.8,
            focal_length=4.25, lens_info="1.54 6.0 1.8 2.4"),
    _preset(11, "Pro Max", software="17.6.1", lens="triple camera", f_number=1.8,
            focal_length=4.25, lens_info="1.54 6.0 1.8 2.4"),
    # --- iPhone 12 ----------------------------------------------------------
    _preset(12, "mini", software="16.6", lens="dual wide camera", f_number=1.6,
            focal_length=4.2, lens_info="1.55 4.2 1.6 2.4"),
    _preset(12, "", software="16.6", lens="dual wide camera", f_number=1.6,
            focal_length=4.2, lens_info="1.549999952 4.2 1.6 2.4"),
    _preset(12, "Pro", software="16.6", lens="triple camera", f_number=1.6,
            focal_length=4.2, lens_info="1.54 6.0 1.6 2.4"),
    _preset(12, "Pro Max", software="16.6", lens="triple camera", f_number=1.6,
            focal_length=5.1, lens_info="1.54 5.1 1.6 2.2"),
    # --- iPhone 13 ----------------------------------------------------------
    _preset(13, "mini", software="17.5.1", lens="dual wide camera", f_number=1.6,
            focal_length=5.1, lens_info="1.57 5.1 1.6 2.4"),
    _preset(13, "", software="17.5.1", lens="dual wide camera", f_number=1.6,
            focal_length=5.1, lens_info="1.57 5.1 1.6 2.4"),
    _preset(13, "Pro", software="17.5.1", lens="triple camera", f_number=1.5,
            focal_length=5.7, lens_info="1.57 9.0 1.5 2.8"),
    _preset(13, "Pro Max", software="17.5.1", lens="triple camera", f_number=1.5,
            focal_length=5.7, lens_info="1.57 9.0 1.5 2.8"),
    # --- iPhone 14 ----------------------------------------------------------
    _preset(14, "", software="17.6.1", lens="dual wide camera", f_number=1.5,
            focal_length=5.7, lens_info="1.57 5.7 1.5 2.4"),
    _preset(14, "Plus", software="17.6.1", lens="dual wide camera", f_number=1.5,
            focal_length=5.7, lens_info="1.57 5.7 1.5 2.4"),
    _preset(14, "Pro", software="17.6.1", lens="triple camera", f_number=1.78,
            focal_length=6.86, focal_length_35mm=24, lens_info="2.22 9.0 1.78 2.8"),
    _preset(14, "Pro Max", software="17.6.1", lens="triple camera", f_number=1.78,
            focal_length=6.86, focal_length_35mm=24, lens_info="2.22 9.0 1.78 2.8"),
    # --- iPhone 15 ----------------------------------------------------------
    _preset(15, "", software="18.5", lens="dual wide camera", f_number=1.6,
            focal_length=5.96, lens_info="1.54 5.96 1.6 2.4"),
    _preset(15, "Plus", software="18.5", lens="dual wide camera", f_number=1.6,
            focal_length=5.96, lens_info="1.54 5.96 1.6 2.4"),
    _preset(15, "Pro", software="18.5", lens="triple camera", f_number=1.78,
            focal_length=6.765, focal_length_35mm=24, lens_info="2.22 9.0 1.78 2.8"),
    _preset(15, "Pro Max", software="18.5", lens="triple camera", f_number=1.78,
            focal_length=6.765, focal_length_35mm=24, lens_info="2.22 15.66 1.78 2.8"),
    # --- iPhone 16 ----------------------------------------------------------
    _preset(16, "", software="18.5", lens="dual wide camera", f_number=1.6,
            focal_length=5.96, lens_info="1.54 5.96 1.6 2.2"),
    _preset(16, "Plus", software="18.5", lens="dual wide camera", f_number=1.6,
            focal_length=5.96, lens_info="1.54 5.96 1.6 2.2"),
    _preset(16, "Pro", software="18.5", lens="triple camera", f_number=1.78,
            focal_length=6.765, focal_length_35mm=24, lens_info="2.22 15.66 1.78 2.8"),
    _preset(16, "Pro Max", software="18.5", lens="triple camera", f_number=1.78,
            focal_length=6.765, focal_length_35mm=24, lens_info="2.22 15.66 1.78 2.8"),
)

# Generations in menu order, newest first - most people want a recent phone.
GENERATIONS: tuple[int, ...] = tuple(
    sorted({preset.generation for preset in DEVICE_PRESETS}, reverse=True)
)


def presets_for_generation(generation: int) -> tuple[DevicePreset, ...]:
    return tuple(p for p in DEVICE_PRESETS if p.generation == generation)


def validate_presets(presets: "tuple[DevicePreset, ...]" = DEVICE_PRESETS) -> None:
    """Fail fast on a malformed catalog (called at import time and in tests)."""
    if not presets:
        raise ValueError("at least one device preset must be configured")

    seen_keys: set[str] = set()
    for preset in presets:
        if not preset.key or not preset.key.replace("_", "").isalnum():
            raise ValueError(f"invalid preset key: {preset.key!r}")
        if len(preset.key) > 32:
            # Keys travel inside Telegram callback data (64 byte budget).
            raise ValueError(f"preset key too long for callback data: {preset.key!r}")
        if preset.key in seen_keys:
            raise ValueError(f"duplicate preset key: {preset.key!r}")
        seen_keys.add(preset.key)
        if not preset.label.strip():
            raise ValueError(f"preset {preset.key!r} has an empty label")
        if not preset.make.strip() or not preset.model.strip():
            raise ValueError(f"preset {preset.key!r} must define make and model")
        if not 10 <= preset.generation <= 99:
            raise ValueError(f"preset {preset.key!r} has an odd generation")
        if preset.f_number <= 0 or preset.focal_length <= 0:
            raise ValueError(f"preset {preset.key!r} needs real camera figures")


def get_preset(key: str) -> DevicePreset | None:
    for preset in DEVICE_PRESETS:
        if preset.key == key:
            return preset
    return None


validate_presets()

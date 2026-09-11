"""Metadata cleaning and rewriting.

Every ExifTool invocation lives here - handlers never build tool arguments.
Both operations work on a *copy* inside the job workspace: ExifTool edits the
container in place, so pixels and encoded streams are never touched and the
resolution cannot change.

Nothing is returned to the user unverified. Each operation reads the file back
and compares it against the snapshot taken before the edit; a write that did
not land raises instead of reporting success.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.data.device_presets import DevicePreset
from app.services.media.capture import ShotParameters, derive_shot
from app.services.media.base import (
    MediaProcessingError,
    ProcessedFile,
    ProcessingErrorCode,
)
from app.services.timeofday import TimeOfDay
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
)

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".3gp"})

# Descriptive/personal tags we strip. Rendering-critical technical tags
# (orientation, colour profile) are copied back so the file still displays
# exactly as before.
_PRESERVED_ON_CLEAN = ("-Orientation", "-ColorSpaceTags", "-ICC_Profile")

# Coordinates survive a round trip through EXIF rationals with far better
# precision than this; the tolerance only absorbs the last-digit rounding.
_GPS_TOLERANCE = 1e-4


@dataclass(frozen=True)
class LocationFix:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class MetadataChangeRequest:
    """Everything the wizard collected, ready to be written."""

    preset: DevicePreset
    taken_at: datetime
    location: LocationFix
    # Drives the exposure figures, so a night shot looks like a night shot.
    time_of_day: TimeOfDay = TimeOfDay.DAY
    # Elevation of the chosen city, written as GPSAltitude.
    altitude: float = 0.0

    @property
    def stamp(self) -> str:
        """ExifTool's timestamp spelling, in the capture's local time."""
        return self.taken_at.strftime("%Y:%m:%d %H:%M:%S")

    @property
    def utc_stamp(self) -> str:
        """The same instant in UTC, which is how QuickTime stores it."""
        return self.taken_at.astimezone(timezone.utc).strftime("%Y:%m:%d %H:%M:%S")

    @property
    def offset_stamp(self) -> str:
        """Local time carrying its UTC offset, e.g. ``2026:09:10 18:42:13-05:00``.

        QuickTime dates are stored in UTC. Handing ExifTool a naive value makes
        it assume the *server's* timezone, so video tags always get this
        explicit form and the stored instant matches the capture location.
        """
        return f"{self.stamp}{self.utc_offset}"

    @property
    def utc_offset(self) -> str:
        raw = self.taken_at.strftime("%z")
        return f"{raw[:3]}:{raw[3:]}" if raw else "+00:00"


@dataclass(frozen=True)
class MetadataSnapshot:
    """What ExifTool reports about a file, before or after an edit."""

    make: str | None = None
    model: str | None = None
    software: str | None = None
    artist: str | None = None
    description: str | None = None
    date_time_original: str | None = None
    create_date: str | None = None
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    width: int | None = None
    height: int | None = None
    orientation: int | None = None

    @property
    def dimensions(self) -> tuple[int | None, int | None]:
        return (self.width, self.height)

    @property
    def timestamp(self) -> str | None:
        """Whichever capture time the format actually carries."""
        return self.date_time_original or self.create_date

    @property
    def identifying_values(self) -> dict[str, str]:
        """Personal/descriptive values still present, for the clean check."""
        candidates = {
            "make": self.make,
            "model": self.model,
            "software": self.software,
            "artist": self.artist,
            "description": self.description,
            "gps_latitude": self.gps_latitude,
            "gps_longitude": self.gps_longitude,
        }
        return {k: str(v) for k, v in candidates.items() if v not in (None, "")}


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def format_iso6709(
    latitude: float, longitude: float, altitude: float | None = None
) -> str:
    """Apple/QuickTime location string, e.g. ``+51.5074+000.1278+011.000/``."""
    # ISO 6709: signed 2-digit latitude, signed 3-digit longitude, and the
    # altitude iPhones append to it.
    text = f"{latitude:+08.4f}{longitude:+09.4f}"
    if altitude is not None:
        text += f"{altitude:+08.3f}"
    return text + "/"


def build_clean_args(exiftool_bin: str, target: Path) -> list[str]:
    """Strip descriptive metadata in place, keeping rendering-critical tags."""
    return [
        exiftool_bin,
        "-overwrite_original",
        "-all=",
        "-tagsFromFile", "@",
        *_PRESERVED_ON_CLEAN,
        "-api", "LargeFileSupport=1",
        "--",
        str(target),
    ]


def build_change_args(
    exiftool_bin: str,
    target: Path,
    request: MetadataChangeRequest,
    shot: ShotParameters,
    *,
    width: int | None = None,
    height: int | None = None,
    orientation: int | None = None,
) -> list[str]:
    """Clear conflicting identity/time/location tags, then write the new ones.

    ExifTool applies assignments left to right, so the ``=``-clears at the front
    guarantee no stale device or GPS data survives underneath.

    ``-n`` makes ExifTool read our values as raw numbers, which is what lets us
    pass ``ExposureProgram=2`` rather than its English spelling.

    Orientation is deliberately left alone: it describes how the stored pixels
    must be rotated for display, so overwriting it would visibly turn the
    user's photo on its side.
    """
    preset = request.preset
    stamp = request.stamp
    offset = request.utc_offset
    lat = request.location.latitude
    lon = request.location.longitude
    lat_ref = "N" if lat >= 0 else "S"
    lon_ref = "E" if lon >= 0 else "W"
    # GPS timestamps are UTC by definition, unlike the EXIF capture time.
    utc = request.taken_at.astimezone(timezone.utc)

    args = [
        exiftool_bin,
        "-overwrite_original",
        "-n",
        "-api", "LargeFileSupport=1",
        "-api", "QuickTimeUTC=1",
        # --- clear conflicting values first --------------------------------
        "-Make=", "-Model=", "-Software=", "-LensModel=",
        "-GPS:all=", "-XMP:all=",
        # Encoder banners and descriptions are someone else's fingerprint.
        "-Comment=", "-ImageDescription=", "-Artist=",
    ]

    if is_video(target):
        coordinates = format_iso6709(lat, lon, shot.gps_altitude)
        args += [
            "-QuickTime:Make=", "-QuickTime:Model=", "-QuickTime:Software=",
            "-Keys:all=", "-UserData:GPSCoordinates=",
            # --- apply -----------------------------------------------------
            # Offset-qualified so the stored UTC instant is the capture's, not
            # one derived from whatever timezone this server runs in.
            f"-QuickTime:CreateDate={request.offset_stamp}",
            f"-QuickTime:ModifyDate={request.offset_stamp}",
            f"-QuickTime:TrackCreateDate={request.offset_stamp}",
            f"-QuickTime:TrackModifyDate={request.offset_stamp}",
            f"-QuickTime:MediaCreateDate={request.offset_stamp}",
            f"-QuickTime:MediaModifyDate={request.offset_stamp}",
            f"-QuickTime:Make={preset.make}",
            f"-QuickTime:Model={preset.model}",
            # The Keys namespace is where an iPhone actually records itself.
            f"-Keys:Make={preset.make}",
            f"-Keys:Model={preset.model}",
            f"-Keys:CreationDate={request.offset_stamp}",
            f"-UserData:GPSCoordinates={coordinates}",
            f"-Keys:GPSCoordinates={coordinates}",
            f"-Keys:LocationAccuracyHorizontal={shot.gps_h_positioning_error}",
        ]
        if preset.software:
            args += [
                f"-QuickTime:Software={preset.software}",
                f"-Keys:Software={preset.software}",
            ]
        for tag, value in preset.quicktime_extra.items():
            args.append(f"-QuickTime:{tag}={value}")
    else:
        args += [
            # --- identity ---------------------------------------------------
            f"-EXIF:Make={preset.make}",
            f"-EXIF:Model={preset.model}",
            # iPhones repeat the model here; its absence is a giveaway.
            f"-EXIF:HostComputer={preset.model}",
            # --- capture time ----------------------------------------------
            f"-EXIF:DateTimeOriginal={stamp}",
            f"-EXIF:CreateDate={stamp}",
            f"-EXIF:ModifyDate={stamp}",
            f"-EXIF:OffsetTime={offset}",
            f"-EXIF:OffsetTimeOriginal={offset}",
            f"-EXIF:OffsetTimeDigitized={offset}",
            f"-EXIF:SubSecTimeOriginal={shot.subsec}",
            f"-EXIF:SubSecTimeDigitized={shot.subsec}",
            # --- exposure ---------------------------------------------------
            f"-EXIF:ExposureTime={shot.exposure_time}",
            f"-EXIF:FNumber={preset.f_number}",
            f"-EXIF:ApertureValue={shot.aperture_value}",
            f"-EXIF:ShutterSpeedValue={shot.shutter_speed_value}",
            f"-EXIF:BrightnessValue={shot.brightness_value}",
            f"-EXIF:ISO={shot.iso}",
            "-EXIF:ExposureProgram=2",      # program AE
            "-EXIF:ExposureCompensation=0",
            "-EXIF:ExposureMode=0",         # auto
            "-EXIF:MeteringMode=5",         # multi-segment
            "-EXIF:Flash=16",               # off, did not fire
            "-EXIF:WhiteBalance=0",         # auto
            "-EXIF:SceneCaptureType=0",     # standard
            "-EXIF:SensingMethod=2",        # one-chip colour area
            "-EXIF:SceneType=1",            # directly photographed
            "-EXIF:CompositeImage=2",       # composited, as Apple records
            # --- lens --------------------------------------------------------
            f"-EXIF:FocalLength={preset.focal_length}",
            f"-EXIF:FocalLengthIn35mmFormat={preset.focal_length_35mm}",
            f"-EXIF:LensInfo={preset.lens_info}",
            f"-EXIF:LensMake={preset.make}",
            f"-EXIF:SubjectArea={shot.subject_area}",
            # --- container / colour -----------------------------------------
            "-EXIF:XResolution=72",
            "-EXIF:YResolution=72",
            "-EXIF:ResolutionUnit=2",       # inches
            "-EXIF:YCbCrPositioning=1",
            "-EXIF:ColorSpace=1",           # sRGB
            "-EXIF:ExifVersion=0232",
            "-EXIF:FlashpixVersion=0100",
            "-EXIF:ComponentsConfiguration=1 2 3 0",
            # --- location ----------------------------------------------------
            "-EXIF:GPSVersionID=2 3 0 0",
            f"-EXIF:GPSLatitude={abs(lat)}",
            f"-EXIF:GPSLatitudeRef={lat_ref}",
            f"-EXIF:GPSLongitude={abs(lon)}",
            f"-EXIF:GPSLongitudeRef={lon_ref}",
            "-EXIF:GPSAltitudeRef=0",       # above sea level
            f"-EXIF:GPSAltitude={shot.gps_altitude}",
            "-EXIF:GPSSpeedRef=K",
            "-EXIF:GPSSpeed=0",
            "-EXIF:GPSImgDirectionRef=T",   # true north
            f"-EXIF:GPSImgDirection={shot.gps_direction}",
            "-EXIF:GPSDestBearingRef=T",
            f"-EXIF:GPSDestBearing={shot.gps_direction}",
            f"-EXIF:GPSHPositioningError={shot.gps_h_positioning_error}",
            f"-EXIF:GPSDateStamp={utc.strftime('%Y:%m:%d')}",
            f"-EXIF:GPSTimeStamp={utc.strftime('%H:%M:%S')}",
        ]
        # Only supplied when the source had none: an absent Orientation reads
        # as 1 anyway, so this adds the tag a real camera writes without ever
        # re-rotating an image that already declared its own.
        if orientation is None:
            args.append("-EXIF:Orientation=1")
        # The pixel dimensions EXIF advertises must match the real ones.
        if width and height:
            args += [
                f"-EXIF:ExifImageWidth={width}",
                f"-EXIF:ExifImageHeight={height}",
            ]
        if preset.software:
            args.append(f"-EXIF:Software={preset.software}")
        if preset.lens_model:
            args.append(f"-EXIF:LensModel={preset.lens_model}")
        for tag, value in preset.exif_extra.items():
            args.append(f"-EXIF:{tag}={value}")

    args += ["--", str(target)]
    return args


def build_read_args(exiftool_bin: str, target: Path) -> list[str]:
    """Read the tags we make promises about, as machine-readable JSON."""
    return [
        exiftool_bin,
        "-json",
        # -n: numbers stay numbers, and GPS comes back signed rather than as
        # "48 deg 51' N", so a snapshot can be compared arithmetically.
        "-n",
        "-api", "QuickTimeUTC=1",
        # Same as the writers: multi-GB videos must read back too.
        "-api", "LargeFileSupport=1",
        "-Make", "-Model", "-Software", "-Artist", "-ImageDescription",
        "-DateTimeOriginal", "-CreateDate",
        "-GPSLatitude", "-GPSLongitude",
        "-ImageWidth", "-ImageHeight", "-Orientation",
        "--",
        str(target),
    ]


# Kept as an alias: the verify step is just a read.
build_verify_args = build_read_args


def _as_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value).strip() or None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_snapshot(payload: str) -> MetadataSnapshot:
    """Turn ``exiftool -json`` output into a :class:`MetadataSnapshot`."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MediaProcessingError(
            ProcessingErrorCode.VERIFY_FAILED, "invalid exiftool json"
        ) from exc
    if not isinstance(data, list) or not data:
        raise MediaProcessingError(
            ProcessingErrorCode.VERIFY_FAILED, "empty exiftool output"
        )

    entry = data[0]
    return MetadataSnapshot(
        make=_as_text(entry.get("Make")),
        model=_as_text(entry.get("Model")),
        software=_as_text(entry.get("Software")),
        artist=_as_text(entry.get("Artist")),
        description=_as_text(entry.get("ImageDescription")),
        date_time_original=_as_text(entry.get("DateTimeOriginal")),
        create_date=_as_text(entry.get("CreateDate")),
        gps_latitude=_as_float(entry.get("GPSLatitude")),
        gps_longitude=_as_float(entry.get("GPSLongitude")),
        width=_as_int(entry.get("ImageWidth")),
        height=_as_int(entry.get("ImageHeight")),
        orientation=_as_int(entry.get("Orientation")),
    )


# ExifTool prints QuickTime dates with a timezone offset once QuickTimeUTC is
# on, and EXIF dates without one.
_STAMP_FORMATS = ("%Y:%m:%d %H:%M:%S%z", "%Y:%m:%d %H:%M:%S")


def parse_stamp(raw: str | None) -> datetime | None:
    """Parse an ExifTool timestamp, with or without a trailing UTC offset."""
    if not raw:
        return None
    for fmt in _STAMP_FORMATS:
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def matches_requested_time(raw: str | None, request: MetadataChangeRequest) -> bool:
    """Whether ``raw`` denotes the instant the wizard asked for.

    An offset-aware value is compared as an absolute instant, so the check does
    not depend on the timezone of the machine running ExifTool. A naive value is
    compared against both spellings, because EXIF stores local time and
    QuickTime stores UTC.
    """
    parsed = parse_stamp(raw)
    if parsed is None:
        return False
    if parsed.tzinfo is not None:
        return abs((parsed - request.taken_at).total_seconds()) < 1
    return raw.strip() in (request.stamp, request.utc_stamp)


def _fail(detail: str) -> None:
    raise MediaProcessingError(ProcessingErrorCode.VERIFY_FAILED, detail)


def _check_dimensions(before: MetadataSnapshot, after: MetadataSnapshot) -> None:
    """The pixels must be untouched; a size change means we transcoded."""
    if before.dimensions != (None, None) and before.dimensions != after.dimensions:
        _fail(f"dimensions changed {before.dimensions} -> {after.dimensions}")


def verify_clean(before: MetadataSnapshot, after: MetadataSnapshot) -> None:
    """Confirm the personal metadata is gone and the picture is intact."""
    remaining = after.identifying_values
    if remaining:
        _fail("metadata still present after clean: " + ", ".join(sorted(remaining)))
    _check_dimensions(before, after)


def verify_change(
    before: MetadataSnapshot, after: MetadataSnapshot, request: MetadataChangeRequest
) -> None:
    """Confirm device, GPS and timestamp really landed, at the original size."""
    preset = request.preset
    if after.make != preset.make or after.model != preset.model:
        _fail(
            f"device not applied: got {after.make!r}/{after.model!r}, "
            f"expected {preset.make!r}/{preset.model!r}"
        )

    for axis, actual, expected in (
        ("latitude", after.gps_latitude, request.location.latitude),
        ("longitude", after.gps_longitude, request.location.longitude),
    ):
        if actual is None or abs(actual - expected) > _GPS_TOLERANCE:
            _fail(f"GPS {axis} not applied: got {actual!r}, expected {expected!r}")

    timestamp = after.timestamp
    if timestamp is None:
        _fail("timestamp missing after change")
    # Matching the requested instant is the stronger claim: it proves the write
    # landed, which "differs from before" alone would not.
    if not matches_requested_time(timestamp, request):
        _fail(
            f"timestamp not applied: got {timestamp!r}, "
            f"expected {request.stamp!r} or {request.utc_stamp!r}"
        )

    _check_dimensions(before, after)


class MetadataService:
    """Async ExifTool front-end for the metadata tools."""

    def __init__(
        self,
        *,
        exiftool_bin: str,
        timeout: float,
        rng: random.Random | None = None,
    ) -> None:
        self._exiftool_bin = exiftool_bin
        self._timeout = timeout
        # Injectable so a test can reproduce an exact shot.
        self._rng = rng or random.Random()

    async def read(self, target: Path, *, job_id: str | None = None) -> MetadataSnapshot:
        """Snapshot a file's metadata without modifying it."""
        payload = await self._run(
            build_read_args(self._exiftool_bin, target),
            job_id=job_id,
            error_code=ProcessingErrorCode.VERIFY_FAILED,
        )
        return parse_snapshot(payload)

    async def clean(self, target: Path, *, job_id: str | None = None) -> ProcessedFile:
        before = await self.read(target, job_id=job_id)
        await self._run(build_clean_args(self._exiftool_bin, target), job_id=job_id)
        processed = self._validate(target, job_id=job_id)

        after = await self.read(target, job_id=job_id)
        verify_clean(before, after)
        logger.info(
            "metadata cleaned, %sx%s preserved",
            after.width,
            after.height,
            extra={"job_id": job_id or "-"},
        )
        return processed

    async def change(
        self, target: Path, request: MetadataChangeRequest, *, job_id: str | None = None
    ) -> ProcessedFile:
        before = await self.read(target, job_id=job_id)

        # The exposure figures depend on the frame size (focus rectangle) and
        # on the hour, so they can only be derived once the file is known.
        shot = derive_shot(
            request.preset,
            request.time_of_day,
            width=before.width,
            height=before.height,
            altitude=request.altitude,
            rng=self._rng,
        )
        await self._run(
            build_change_args(
                self._exiftool_bin,
                target,
                request,
                shot,
                width=before.width,
                height=before.height,
                orientation=before.orientation,
            ),
            job_id=job_id,
        )
        processed = self._validate(target, job_id=job_id)

        after = await self.read(target, job_id=job_id)
        verify_change(before, after, request)
        logger.info(
            "metadata changed to %s (ISO %s, 1/%ss), %sx%s preserved",
            request.preset.model,
            shot.iso,
            round(1 / shot.exposure_time),
            after.width,
            after.height,
            extra={"job_id": job_id or "-"},
        )
        return processed

    async def _run(
        self,
        args: list[str],
        *,
        job_id: str | None,
        error_code: ProcessingErrorCode = ProcessingErrorCode.METADATA_FAILED,
    ) -> str:
        try:
            result = await run_command(args, timeout=self._timeout, job_id=job_id)
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            raise MediaProcessingError(error_code, str(exc)) from exc
        return result.stdout

    @staticmethod
    def _validate(target: Path, *, job_id: str | None) -> ProcessedFile:
        if not target.exists() or target.stat().st_size == 0:
            raise MediaProcessingError(ProcessingErrorCode.EMPTY_OUTPUT, "empty output")
        logger.info(
            "metadata output ready (%d bytes)",
            target.stat().st_size,
            extra={"job_id": job_id or "-"},
        )
        return ProcessedFile(
            path=target, size_bytes=target.stat().st_size, filename=target.name
        )

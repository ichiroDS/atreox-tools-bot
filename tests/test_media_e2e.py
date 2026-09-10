"""End-to-end media tests against the real native tools.

They are skipped when the tool is missing, so the suite still runs on a bare
machine; inside the Docker image (ffmpeg + exiftool installed) they all run.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.data.device_presets import get_preset
from app.services.media.circle import FfmpegVideoCircleService
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.metadata import (
    LocationFix,
    MetadataChangeRequest,
    MetadataService,
    build_verify_args,
)
from app.services.media.probe import MediaProbe
from app.utils.binaries import is_available, resolve_binary
from app.utils.subprocess import run_command

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
# Resolved the same way the bot resolves it, so a Windows install that is only
# on the *user* PATH still exercises these tests.
EXIFTOOL = resolve_binary("exiftool")
needs_exiftool = pytest.mark.skipif(
    not is_available("exiftool"), reason="exiftool not installed"
)


async def make_video(path: Path, *, width=640, height=360, seconds=2) -> Path:
    await run_command(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", str(seconds), "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        timeout=120,
    )
    return path


async def make_image(path: Path, *, width=320, height=240) -> Path:
    await run_command(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={width}x{height}",
            "-frames:v", "1", str(path),
        ],
        timeout=120,
    )
    return path


# Metadata a real camera/phone would leave behind, seeded so "clean" has
# something genuine to remove.
_ORIGINAL_TAGS = [
    "-EXIF:Make=Samsung",
    "-EXIF:Model=SM-G991B",
    "-EXIF:Software=Android 12",
    "-EXIF:Artist=Some Person",
    "-EXIF:ImageDescription=holiday photo",
    "-EXIF:DateTimeOriginal=2021:03:04 08:15:00",
    "-EXIF:GPSLatitude=48.85",
    "-EXIF:GPSLatitudeRef=N",
    "-EXIF:GPSLongitude=2.35",
    "-EXIF:GPSLongitudeRef=E",
    "-XMP:Creator=Some Person",
]


async def seed_metadata(path: Path) -> Path:
    await run_command(
        [EXIFTOOL, "-overwrite_original", *_ORIGINAL_TAGS, "--", str(path)], timeout=60
    )
    return path


@needs_ffmpeg
async def test_probe_reads_a_real_file(tmp_path):
    source = await make_video(tmp_path / "in.mp4")
    info = await MediaProbe("ffprobe").probe(source)
    assert (info.width, info.height) == (640, 360)
    assert info.has_audio is True
    assert 1.5 < info.duration < 2.5


@needs_ffmpeg
async def test_circle_conversion_produces_a_square_video(tmp_path):
    source = await make_video(tmp_path / "in.mp4")
    destination = tmp_path / "out.mp4"

    service = FfmpegVideoCircleService(
        ffmpeg_bin="ffmpeg",
        probe=MediaProbe("ffprobe"),
        size=384,
        max_duration=60,
        timeout=120,
    )
    result = await service.to_circle(source, destination)

    assert result.size_bytes > 0
    info = await MediaProbe("ffprobe").probe(destination)
    assert (info.width, info.height) == (384, 384)
    assert info.video_codec == "h264"
    assert info.has_audio is True


@needs_ffmpeg
async def test_circle_conversion_trims_to_the_duration_limit(tmp_path):
    source = await make_video(tmp_path / "long.mp4", seconds=5)
    destination = tmp_path / "short.mp4"

    service = FfmpegVideoCircleService(
        ffmpeg_bin="ffmpeg",
        probe=MediaProbe("ffprobe"),
        size=256,
        max_duration=2,
        timeout=120,
    )
    await service.to_circle(source, destination)
    assert (await MediaProbe("ffprobe").probe(destination)).duration <= 2.5


@needs_ffmpeg
async def test_circle_conversion_handles_a_silent_source(tmp_path):
    source = tmp_path / "silent.mp4"
    await run_command(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=480x640:rate=30",
            "-t", "2", "-pix_fmt", "yuv420p", str(source),
        ],
        timeout=120,
    )
    destination = tmp_path / "out.mp4"
    service = FfmpegVideoCircleService(
        ffmpeg_bin="ffmpeg",
        probe=MediaProbe("ffprobe"),
        size=384,
        max_duration=60,
        timeout=120,
    )
    await service.to_circle(source, destination)
    info = await MediaProbe("ffprobe").probe(destination)
    assert (info.width, info.height) == (384, 384)
    assert info.has_audio is False


async def read_tags(path: Path) -> dict:
    result = await run_command(build_verify_args(EXIFTOOL, path), timeout=60)
    return json.loads(result.stdout)[0]


def service() -> MetadataService:
    return MetadataService(exiftool_bin=EXIFTOOL, timeout=120)


def change_request(**overrides) -> MetadataChangeRequest:
    defaults = dict(
        preset=get_preset("iphone_16_pro"),
        taken_at=datetime(2026, 5, 17, 19, 30, 0, tzinfo=timezone(timedelta(hours=3))),
        location=LocationFix(latitude=50.4501, longitude=30.5234),
    )
    return MetadataChangeRequest(**{**defaults, **overrides})


# (name, maker, expected dimensions) covering both orientations per format.
async def build_fixture(tmp_path: Path, kind: str) -> tuple[Path, tuple[int, int]]:
    if kind == "jpeg_landscape":
        return await seed_metadata(await make_image(tmp_path / "l.jpg", width=640, height=480)), (640, 480)
    if kind == "jpeg_portrait":
        return await seed_metadata(await make_image(tmp_path / "p.jpg", width=480, height=640)), (480, 640)
    if kind == "png":
        return await seed_metadata(await make_image(tmp_path / "a.png", width=320, height=240)), (320, 240)
    if kind == "mp4_landscape":
        return await make_video(tmp_path / "l.mp4", width=640, height=360), (640, 360)
    if kind == "mp4_portrait":
        return await make_video(tmp_path / "p.mp4", width=360, height=640), (360, 640)
    raise AssertionError(f"unknown fixture {kind}")


ALL_FIXTURES = [
    "jpeg_landscape",
    "jpeg_portrait",
    "png",
    "mp4_landscape",
    "mp4_portrait",
]
IMAGE_FIXTURES = ["jpeg_landscape", "jpeg_portrait", "png"]


@needs_exiftool
@needs_ffmpeg
@pytest.mark.parametrize("kind", ALL_FIXTURES)
async def test_clean_removes_personal_metadata_at_the_original_size(kind, tmp_path):
    path, dimensions = await build_fixture(tmp_path, kind)
    before = await service().read(path)

    await service().clean(path)

    after = await service().read(path)
    # Nothing identifying survives...
    assert after.identifying_values == {}
    # ...and the picture itself is untouched.
    assert after.dimensions == dimensions
    assert before.dimensions == after.dimensions


@needs_exiftool
@needs_ffmpeg
@pytest.mark.parametrize("kind", IMAGE_FIXTURES)
async def test_clean_removes_the_specific_tags_that_were_there(kind, tmp_path):
    path, _ = await build_fixture(tmp_path, kind)
    seeded = await read_tags(path)
    assert seeded["Make"] == "Samsung"
    assert seeded["GPSLatitude"] == 48.85

    await service().clean(path)

    tags = await read_tags(path)
    for tag in ("Make", "Model", "Software", "Artist", "ImageDescription",
                "GPSLatitude", "GPSLongitude", "DateTimeOriginal"):
        assert tag not in tags, f"{tag} survived the clean"


@needs_exiftool
@needs_ffmpeg
@pytest.mark.parametrize("kind", ALL_FIXTURES)
async def test_change_writes_device_gps_and_time_at_the_original_size(kind, tmp_path):
    path, dimensions = await build_fixture(tmp_path, kind)
    request = change_request()
    before = await service().read(path)

    await service().change(path, request)

    after = await service().read(path)
    assert after.make == "Apple"
    assert after.model == "iPhone 16 Pro"
    assert round(after.gps_latitude, 4) == 50.4501
    assert round(after.gps_longitude, 4) == 30.5234
    assert after.timestamp != before.timestamp
    # Resolution must survive untouched - we never transcode.
    assert after.dimensions == dimensions


@needs_exiftool
@needs_ffmpeg
@pytest.mark.parametrize("kind", ["mp4_landscape", "mp4_portrait"])
async def test_video_streams_are_remuxed_not_re_encoded(kind, tmp_path):
    path, dimensions = await build_fixture(tmp_path, kind)

    async def stream_digest() -> str:
        result = await run_command(
            ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy",
             "-f", "md5", "-"],
            timeout=120,
        )
        return result.stdout.strip()

    before_digest = await stream_digest()
    await service().change(path, change_request())
    assert await stream_digest() == before_digest

    await service().clean(path)
    assert await stream_digest() == before_digest

    info = await MediaProbe("ffprobe").probe(path)
    assert (info.width, info.height) == dimensions


@needs_exiftool
@needs_ffmpeg
@pytest.mark.parametrize("kind", IMAGE_FIXTURES)
async def test_images_are_never_recompressed(kind, tmp_path):
    path, _ = await build_fixture(tmp_path, kind)

    async def pixel_digest() -> str:
        result = await run_command(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "md5", "-"], timeout=120
        )
        return result.stdout.strip()

    before_digest = await pixel_digest()
    await service().change(path, change_request())
    assert await pixel_digest() == before_digest

    await service().clean(path)
    assert await pixel_digest() == before_digest


@needs_exiftool
@needs_ffmpeg
@pytest.mark.parametrize(
    "offset_hours", [-8, 0, 3, 9], ids=["utc-8", "utc", "utc+3", "utc+9"]
)
async def test_video_timestamps_do_not_depend_on_the_server_timezone(
    offset_hours, tmp_path
):
    # QuickTime stores UTC. If the capture offset were ignored, the stored
    # instant would silently follow whatever timezone this machine runs in.
    path, _ = await build_fixture(tmp_path, "mp4_landscape")
    request = change_request(
        taken_at=datetime(
            2026, 5, 17, 19, 30, 0, tzinfo=timezone(timedelta(hours=offset_hours))
        )
    )
    await service().change(path, request)  # verification raises on a mismatch


@needs_exiftool
@needs_ffmpeg
async def test_change_on_a_southern_western_location(tmp_path):
    path, _ = await build_fixture(tmp_path, "jpeg_landscape")
    await service().change(
        path, change_request(location=LocationFix(latitude=-33.8688, longitude=-70.6693))
    )

    after = await service().read(path)
    assert round(after.gps_latitude, 4) == -33.8688
    assert round(after.gps_longitude, 4) == -70.6693


@needs_exiftool
async def test_a_corrupt_file_fails_cleanly_instead_of_crashing(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"this is not an image")

    with pytest.raises(MediaProcessingError) as excinfo:
        await service().clean(broken)
    # Mapped to a stable code the bot turns into friendly copy.
    assert excinfo.value.code in {
        ProcessingErrorCode.METADATA_FAILED,
        ProcessingErrorCode.VERIFY_FAILED,
    }


@needs_exiftool
@needs_ffmpeg
async def test_verification_rejects_a_write_that_did_not_land(tmp_path):
    """The promise is verified, not assumed."""
    path, _ = await build_fixture(tmp_path, "jpeg_landscape")
    request = change_request()
    await service().change(path, request)

    # Same file, but now claim a different device was written.
    mismatched = change_request(preset=get_preset("iphone_13_pro"))
    after = await service().read(path)
    from app.services.media.metadata import verify_change

    with pytest.raises(MediaProcessingError) as excinfo:
        verify_change(after, after, mismatched)
    assert excinfo.value.code is ProcessingErrorCode.VERIFY_FAILED

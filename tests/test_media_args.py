from __future__ import annotations

import random

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.data.device_presets import get_preset
from app.services.media.base import MediaProcessingError
from app.services.media.circle import build_circle_ffmpeg_args
from app.services.media.capture import derive_shot
from app.services.timeofday import TimeOfDay
from app.services.media.metadata import (
    LocationFix,
    MetadataChangeRequest,
    build_change_args,
    build_clean_args,
    build_verify_args,
    format_iso6709,
    is_video,
)
from app.services.media.probe import build_ffprobe_args, parse_probe_output

SRC = Path("/tmp/atreox-tools/job/src_abc.mp4")
DST = Path("/tmp/atreox-tools/job/circle_abc.mp4")


# --- circle -----------------------------------------------------------------


def test_circle_args_are_an_argv_array_not_a_shell_string():
    args = build_circle_ffmpeg_args("ffmpeg", SRC, DST, size=384, duration_limit=30)
    assert all(isinstance(a, str) for a in args)
    assert args[0] == "ffmpeg"
    assert args[-1] == str(DST)


def test_circle_args_crop_square_and_scale():
    args = build_circle_ffmpeg_args("ffmpeg", SRC, DST, size=384, duration_limit=30)
    video_filter = args[args.index("-vf") + 1]
    assert "crop=" in video_filter
    assert "min(iw,ih)" in video_filter
    assert "scale=384:384" in video_filter
    assert "setsar=1" in video_filter


def test_circle_duration_is_clamped_to_the_telegram_limit():
    args = build_circle_ffmpeg_args("ffmpeg", SRC, DST, size=384, duration_limit=600)
    assert float(args[args.index("-t") + 1]) == 60.0


def test_circle_keeps_a_shorter_duration():
    args = build_circle_ffmpeg_args("ffmpeg", SRC, DST, size=384, duration_limit=12.5)
    assert float(args[args.index("-t") + 1]) == 12.5


def test_circle_drops_audio_when_the_source_has_none():
    args = build_circle_ffmpeg_args(
        "ffmpeg", SRC, DST, size=384, duration_limit=10, with_audio=False
    )
    assert "-an" in args
    assert "-c:a" not in args


def test_circle_encodes_telegram_compatible_video():
    args = build_circle_ffmpeg_args("ffmpeg", SRC, DST, size=384, duration_limit=10)
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    assert args[args.index("-movflags") + 1] == "+faststart"


@pytest.mark.parametrize("size", [0, -2, 385])
def test_circle_rejects_invalid_sizes(size):
    with pytest.raises(ValueError):
        build_circle_ffmpeg_args("ffmpeg", SRC, DST, size=size, duration_limit=10)


# --- probe ------------------------------------------------------------------


def test_ffprobe_args_terminate_options_before_the_path():
    args = build_ffprobe_args("ffprobe", SRC)
    assert args[-2:] == ["--", str(SRC)]


def _probe_payload(**video):
    stream = {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080}
    stream.update(video)
    return json.dumps({"streams": [stream], "format": {"duration": "12.5"}})


def test_parse_probe_output_reads_dimensions_and_duration():
    info = parse_probe_output(_probe_payload(duration="9.5"))
    assert (info.width, info.height) == (1920, 1080)
    assert info.duration == 9.5
    assert info.has_audio is False
    assert info.square_side == 1080


def test_parse_probe_output_falls_back_to_container_duration():
    info = parse_probe_output(_probe_payload())
    assert info.duration == 12.5


def test_parse_probe_output_honours_rotation_tags():
    info = parse_probe_output(_probe_payload(tags={"rotate": "-90"}))
    assert info.rotation == 90
    assert (info.display_width, info.display_height) == (1080, 1920)


def test_parse_probe_output_honours_rotation_side_data():
    info = parse_probe_output(_probe_payload(side_data_list=[{"rotation": -270}]))
    assert info.rotation == 270


def test_parse_probe_output_detects_audio():
    payload = json.dumps(
        {
            "streams": [
                {"codec_type": "video", "width": 100, "height": 100, "duration": "1"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {},
        }
    )
    assert parse_probe_output(payload).has_audio is True


def test_parse_probe_output_rejects_files_without_video():
    payload = json.dumps({"streams": [{"codec_type": "audio"}], "format": {}})
    with pytest.raises(MediaProcessingError):
        parse_probe_output(payload)


def test_parse_probe_output_rejects_garbage():
    with pytest.raises(MediaProcessingError):
        parse_probe_output("not json")


# --- metadata ---------------------------------------------------------------


def test_clean_args_strip_all_but_keep_rendering_tags():
    target = Path("/tmp/x/clean_1.jpg")
    args = build_clean_args("exiftool", target)
    assert "-all=" in args
    assert "-tagsFromFile" in args
    assert "-Orientation" in args
    assert "-ICC_Profile" in args
    assert "-overwrite_original" in args
    assert args[-2:] == ["--", str(target)]


def _request(**overrides):
    defaults = dict(
        preset=get_preset("iphone_15_pro"),
        taken_at=datetime(2026, 5, 17, 9, 30, 0, tzinfo=timezone(timedelta(hours=2))),
        location=LocationFix(latitude=50.4501, longitude=30.5234),
        time_of_day=TimeOfDay.DAY,
        altitude=180.0,
    )
    return MetadataChangeRequest(**{**defaults, **overrides})


def _shot(request=None):
    """A reproducible shot, so argument assertions stay deterministic."""
    request = request or _request()
    return derive_shot(
        request.preset,
        request.time_of_day,
        width=4032,
        height=3024,
        altitude=request.altitude,
        rng=random.Random(0),
    )


def _change_args(path, request=None, **kwargs):
    request = request or _request()
    return build_change_args(
        "exiftool", Path(path), request, _shot(request), **kwargs
    )


def test_change_args_for_images_write_exif():
    args = _change_args("/tmp/x/a.jpg")
    assert "-EXIF:Make=Apple" in args
    assert "-EXIF:Model=iPhone 15 Pro" in args
    assert "-EXIF:DateTimeOriginal=2026:05:17 09:30:00" in args
    assert "-EXIF:OffsetTimeOriginal=+02:00" in args
    assert "-EXIF:GPSLatitudeRef=N" in args
    assert "-EXIF:GPSLongitudeRef=E" in args


def test_change_args_clear_conflicting_tags_before_writing():
    args = _change_args("/tmp/x/a.jpg")
    assert args.index("-Make=") < args.index("-EXIF:Make=Apple")
    assert args.index("-GPS:all=") < args.index("-EXIF:GPSLatitude=50.4501")
    assert "-XMP:all=" in args


def test_change_args_for_videos_write_quicktime_not_exif():
    args = _change_args("/tmp/x/a.mov")
    assert "-QuickTime:Make=Apple" in args
    # QuickTime dates carry an explicit offset so the stored UTC instant does
    # not depend on the server's timezone.
    assert "-QuickTime:CreateDate=2026:05:17 09:30:00+02:00" in args
    assert "-Keys:CreationDate=2026:05:17 09:30:00+02:00" in args
    assert not any(a.startswith("-EXIF:Make=") for a in args)
    assert args.index("-QuickTime:Make=") < args.index("-QuickTime:Make=Apple")


def test_change_args_write_southern_and_western_hemispheres():
    request = _request(
        preset=get_preset("iphone_14_pro"),
        taken_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        location=LocationFix(latitude=-33.8688, longitude=-70.6693),
    )
    args = _change_args("/tmp/x/a.jpg", request)
    assert "-EXIF:GPSLatitudeRef=S" in args
    assert "-EXIF:GPSLongitudeRef=W" in args
    assert "-EXIF:GPSLatitude=33.8688" in args


def test_change_args_never_lose_the_path_terminator():
    args = _change_args("/tmp/x/-weird-name.jpg")
    assert args[-2] == "--"


def test_change_args_write_the_full_iphone_exif_block():
    """A believable photo needs the whole block, not just make and model."""
    args = _change_args("/tmp/x/a.jpg", width=4032, height=3024)
    written = {a.split("=", 1)[0] for a in args if a.startswith("-EXIF:") and "=" in a}
    for tag in (
        "-EXIF:HostComputer", "-EXIF:ExposureTime", "-EXIF:FNumber", "-EXIF:ISO",
        "-EXIF:ApertureValue", "-EXIF:ShutterSpeedValue", "-EXIF:BrightnessValue",
        "-EXIF:ExposureProgram", "-EXIF:MeteringMode", "-EXIF:Flash",
        "-EXIF:ExposureMode", "-EXIF:WhiteBalance", "-EXIF:SceneCaptureType",
        "-EXIF:SensingMethod", "-EXIF:SceneType", "-EXIF:CompositeImage",
        "-EXIF:FocalLengthIn35mmFormat", "-EXIF:LensInfo", "-EXIF:LensMake",
        "-EXIF:LensModel", "-EXIF:SubjectArea", "-EXIF:ColorSpace",
        "-EXIF:ExifVersion", "-EXIF:FlashpixVersion", "-EXIF:ComponentsConfiguration",
        "-EXIF:SubSecTimeOriginal", "-EXIF:OffsetTimeDigitized",
        "-EXIF:GPSVersionID", "-EXIF:GPSAltitude", "-EXIF:GPSAltitudeRef",
        "-EXIF:GPSSpeed", "-EXIF:GPSImgDirection", "-EXIF:GPSDestBearing",
        "-EXIF:GPSHPositioningError",
    ):
        assert tag in written, f"{tag} is missing from the EXIF block"


def test_change_args_advertise_the_real_pixel_dimensions():
    args = _change_args("/tmp/x/a.jpg", width=4032, height=3024)
    assert "-EXIF:ExifImageWidth=4032" in args
    assert "-EXIF:ExifImageHeight=3024" in args


def test_change_args_pass_values_as_raw_numbers():
    # Without -n, ExifTool would expect "Program AE" instead of 2.
    args = _change_args("/tmp/x/a.jpg")
    assert "-n" in args
    assert "-EXIF:ExposureProgram=2" in args


def test_change_args_leave_an_existing_orientation_alone():
    """Rewriting Orientation would visibly rotate the user's photo."""
    kept = _change_args("/tmp/x/a.jpg", orientation=6)
    assert not any(a.startswith("-EXIF:Orientation=") for a in kept)
    # ...but a file with none gets the tag a real camera writes.
    backfilled = _change_args("/tmp/x/a.jpg", orientation=None)
    assert "-EXIF:Orientation=1" in backfilled


def test_change_args_gps_timestamp_is_utc_not_local():
    # Capture is 09:30 at +02:00, so the GPS clock reads 07:30 UTC.
    args = _change_args("/tmp/x/a.jpg")
    assert "-EXIF:GPSTimeStamp=07:30:00" in args
    assert "-EXIF:GPSDateStamp=2026:05:17" in args


def test_video_gets_the_apple_keys_block_with_altitude():
    args = _change_args("/tmp/x/a.mov")
    assert "-Keys:Make=Apple" in args
    assert "-Keys:Model=iPhone 15 Pro" in args
    assert any(a.startswith("-Keys:LocationAccuracyHorizontal=") for a in args)
    coordinates = [a for a in args if a.startswith("-Keys:GPSCoordinates=")][0]
    # ISO 6709 with the altitude an iPhone appends.
    assert coordinates.endswith("/")
    assert coordinates.count("+") + coordinates.count("-") >= 3


@pytest.mark.parametrize(
    "lat,lon,expected",
    [
        (50.4501, 30.5234, "+50.4501+030.5234/"),
        (-33.8688, -70.6693, "-33.8688-070.6693/"),
        (0.0, 0.0, "+00.0000+000.0000/"),
    ],
)
def test_format_iso6709(lat, lon, expected):
    assert format_iso6709(lat, lon) == expected


@pytest.mark.parametrize(
    "name,expected",
    [("a.mp4", True), ("a.MOV", True), ("a.jpg", False), ("a.png", False)],
)
def test_is_video(name, expected):
    assert is_video(Path(name)) is expected


def test_verify_args_read_back_the_written_tags():
    args = build_verify_args("exiftool", Path("/tmp/x/a.jpg"))
    assert "-json" in args
    for tag in ("-Make", "-Model", "-DateTimeOriginal", "-GPSLatitude"):
        assert tag in args

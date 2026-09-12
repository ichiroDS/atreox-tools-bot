"""Phase 9: the Watermark tool.

Real media throughout - Pillow draws the photos, FFmpeg the videos, and every
output is checked with ffprobe and, for photos, pixel by pixel. Where the
watermark landed is measured from the picture itself: a lossless PNG tells us
exactly which pixels changed, so "bottom right" is a fact, not a hope.

Presets are exercised against a real SQLite session, including the per-user
limit and the isolation between two users.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import func, select

from app.bot import texts
from app.bot.callbacks import MenuCallback, WatermarkCallback
from app.bot.keyboards.common import main_menu
from app.bot.keyboards.watermark import preset_detail, preset_list, watermark_done
from app.bot.routers import watermark as watermark_router
from app.bot.states import WatermarkStates
from app.db.models import MAX_PRESETS_PER_USER, Feature, FeatureEvent, JobType, WatermarkPreset
from app.db.repositories import (
    DuplicateName,
    JobsRepository,
    PresetLimitReached,
    StatsRepository,
    WatermarkPresetsRepository,
)
from app.services import telegram_files
from app.services.media import watermark as watermark_media
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.watermark import (
    MAX_TEXT_LENGTH,
    OPACITIES,
    Position,
    Size,
    Style,
    WatermarkService,
    WatermarkSpec,
    build_drawtext,
    clean_watermark_text,
    fit_font_size,
    padding_for,
    plan_layout,
    plan_video,
    resolve_font,
    watermarked_filename,
)
from app.services.media.optimizer import MediaKind
from app.services.telegram_files import FileKind, IncomingFile
from app.utils.temp_files import JobWorkspace
from tests.test_large_files import (
    MB,
    SERVER_FILE,
    FakeBot,
    FakeFileServer,
    FakeJobs,
    local_settings,
    workspaces_left,
)
from tests.test_media_optimizer import FakeCallback, FakeMessage, fsm_for, probe

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageChops  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def _font_available() -> bool:
    """Drawing needs a TrueType font; a bare machine may not have one."""
    try:
        resolve_font()
    except MediaProcessingError:
        return False
    return True


needs_font = pytest.mark.skipif(not _font_available(), reason="no watermark font on this system")


# --- helpers ------------------------------------------------------------------


class WizardMessage(FakeMessage):
    """Like the shared fake, but remembers the keyboard of every edit too -
    the watermark wizard edits one message all the way through."""

    async def edit_text(self, text=None, **kwargs):
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return self


def service(timeout: float = 300) -> WatermarkService:
    return WatermarkService(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe",
                            timeout=timeout, probe_timeout=60)


def workspace_in(tmp_path: Path) -> JobWorkspace:
    path = tmp_path / f"ws-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return JobWorkspace(job_id=uuid.uuid4(), path=path)


def changed_box(before: Path, after: Path) -> tuple[int, int, int, int] | None:
    """Which pixels differ - exact for a lossless format."""
    with Image.open(before) as first, Image.open(after) as second:
        return ImageChops.difference(first.convert("RGB"), second.convert("RGB")).getbbox()


def region_of(box: tuple[int, int, int, int], size: tuple[int, int]) -> str:
    """Name the part of the frame a bounding box sits in."""
    width, height = size
    left, top, right, bottom = box
    horizontal = "left" if right <= width * 0.55 else ("right" if left >= width * 0.45 else "center")
    vertical = "top" if bottom <= height * 0.55 else "bottom"
    return f"{vertical}_{horizontal}"


def brightness(path: Path, box: tuple[int, int, int, int]) -> float:
    with Image.open(path) as image:
        return sum(image.convert("L").crop(box).getdata()) / max(1, (box[2] - box[0]) * (box[3] - box[1]))


def _photo(width: int, height: int, colour: str = "black") -> Image.Image:
    return Image.new("RGB", (width, height), colour)


def frame_at(video: Path, seconds: float, destination: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(seconds),
         "-i", str(video), "-frames:v", "1", str(destination)],
        check=True, timeout=120,
    )
    return destination


# --- real photos ----------------------------------------------------------------


@pytest.fixture(scope="module")
def photos(tmp_path_factory):
    root = tmp_path_factory.mktemp("watermark-photos")
    cache: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name in cache:
            return cache[name]
        folder = root / name
        folder.mkdir()
        if name == "landscape_jpeg":
            path = folder / "landscape.jpg"
            _photo(1600, 1200).save(path, quality=95)
        elif name == "portrait_jpeg":
            path = folder / "portrait.jpg"
            _photo(1200, 1600).save(path, quality=95)
        elif name == "png":
            path = folder / "flat.png"
            _photo(1200, 900).save(path)
        elif name == "transparent_png":
            path = folder / "logo.png"
            picture = Image.new("RGBA", (1000, 800), (0, 0, 0, 0))
            picture.paste((10, 10, 10, 255), (0, 0, 500, 800))
            picture.save(path)
        elif name == "webp":
            path = folder / "shot.webp"
            _photo(1200, 900).save(path, quality=92)
        elif name == "rotated_jpeg":
            # Stored landscape, displayed portrait (EXIF orientation 6).
            path = folder / "IMG_9000.jpg"
            exif = Image.Exif()
            exif[0x0112] = 6
            _photo(1600, 1200).save(path, quality=95, exif=exif.tobytes())
        else:
            raise AssertionError(name)
        cache[name] = path
        return path

    return get


@needs_ffmpeg
@needs_font
@pytest.mark.parametrize("name,dimensions", [
    ("landscape_jpeg", (1600, 1200)),
    ("portrait_jpeg", (1200, 1600)),
    ("png", (1200, 900)),
    ("webp", (1200, 900)),
])
async def test_a_photo_keeps_its_format_and_exact_dimensions(name, dimensions, photos, tmp_path):
    source = photos(name)
    analysis = await service().analyze(source)
    result = await service().apply(source, workspace_in(tmp_path), analysis,
                                   WatermarkSpec("@lunaaa"))

    with Image.open(source) as before, Image.open(result.path) as after:
        assert after.size == before.size == dimensions
        assert after.format == before.format
    assert (result.width, result.height) == dimensions
    written = probe(result.path)
    assert (written["width"], written["height"]) == dimensions


@needs_ffmpeg
@needs_font
@pytest.mark.parametrize("position", list(Position))
async def test_every_position_lands_where_it_says(position, photos, tmp_path):
    source = photos("png")          # lossless: the changed pixels are exact
    analysis = await service().analyze(source)
    result = await service().apply(source, workspace_in(tmp_path), analysis,
                                   WatermarkSpec("@lunaaa", position=position))

    box = changed_box(source, result.path)
    assert box is not None, "nothing was drawn"
    assert region_of(box, (analysis.width, analysis.height)) == position.value
    # ...and it sits inside the frame, with its padding kept.
    padding = padding_for(analysis.width, analysis.height)
    assert box[0] >= padding - 1 and box[1] >= padding - 1
    assert box[2] <= analysis.width - padding + 1
    assert box[3] <= analysis.height - padding + 1


@needs_ffmpeg
@needs_font
async def test_size_grows_with_the_preset(photos, tmp_path):
    source = photos("png")
    analysis = await service().analyze(source)
    heights = []
    for size in (Size.S, Size.M, Size.L):
        result = await service().apply(source, workspace_in(tmp_path), analysis,
                                       WatermarkSpec("@lunaaa", size=size))
        box = changed_box(source, result.path)
        heights.append(box[3] - box[1])
    assert heights[0] < heights[1] < heights[2]
    # Roughly the promised share of the frame height (2.5 / 3.5 / 5 %).
    assert heights[1] == pytest.approx(analysis.height * 0.035, rel=0.6)


@needs_ffmpeg
@needs_font
async def test_opacity_changes_how_strongly_it_shows(photos, tmp_path):
    source = photos("png")          # solid black, so brighter means more ink
    analysis = await service().analyze(source)
    readings = []
    for opacity in OPACITIES:
        result = await service().apply(
            source, workspace_in(tmp_path), analysis,
            WatermarkSpec("@lunaaa", style=Style.WHITE, opacity=opacity),
        )
        box = changed_box(source, result.path)
        readings.append(brightness(result.path, box))
    assert readings == sorted(readings)
    assert readings[0] < readings[-1]


@needs_ffmpeg
@needs_font
async def test_transparency_survives_and_only_the_watermark_changes(photos, tmp_path):
    source = photos("transparent_png")
    analysis = await service().analyze(source)
    assert analysis.has_alpha

    result = await service().apply(source, workspace_in(tmp_path), analysis,
                                   WatermarkSpec("@mia", position=Position.TOP_LEFT))

    with Image.open(result.path) as after, Image.open(source) as before:
        assert after.mode == "RGBA"
        assert after.size == before.size
        # The transparent half is still transparent outside the watermark.
        assert after.getpixel((900, 700))[3] == 0
        assert after.crop((0, 200, 1000, 800)).tobytes() == before.crop((0, 200, 1000, 800)).tobytes()
    assert probe(result.path)["pix_fmt"] == "rgba"


@needs_ffmpeg
@needs_font
async def test_a_rotated_photo_is_watermarked_the_right_way_up(photos, tmp_path):
    source = photos("rotated_jpeg")   # stored 1600x1200, displayed 1200x1600
    analysis = await service().analyze(source)
    result = await service().apply(source, workspace_in(tmp_path), analysis,
                                   WatermarkSpec("@luna"))

    with Image.open(result.path) as after:
        # Written upright: the displayed shape is unchanged, the stored one swaps.
        assert after.size == (1200, 1600)
        # The pixels are upright now, so no orientation tag is left to apply.
        assert after.getexif().get(0x0112, 1) == 1
    assert result.width * result.height == analysis.width * analysis.height


@needs_ffmpeg
@needs_font
async def test_a_jpeg_is_written_at_high_quality_without_personal_metadata(photos, tmp_path):
    source = photos("rotated_jpeg")
    analysis = await service().analyze(source)
    result = await service().apply(source, workspace_in(tmp_path), analysis, WatermarkSpec("@x"))
    with Image.open(result.path) as after:
        assert after.format == "JPEG"
        # 4:4:4 keeps the text edges crisp...
        assert probe(result.path)["pix_fmt"] == "yuvj444p"
        # ...and the orientation tag is gone because the pixels are upright now.
        assert after.getexif().get(0x0112) in (None, 1)


@needs_ffmpeg
@needs_font
async def test_an_animated_image_is_refused(tmp_path):
    folder = tmp_path / "server"
    folder.mkdir()
    path = folder / "sticker.webp"
    frames = [Image.new("RGB", (64, 64), colour) for colour in ("red", "green", "blue")]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(MediaProcessingError) as excinfo:
        analysis = await service().analyze(path)
        await service().apply(path, workspace_in(tmp_path), analysis, WatermarkSpec("@x"))
    assert excinfo.value.code in (ProcessingErrorCode.UNSUPPORTED, ProcessingErrorCode.PROBE_FAILED)


# --- real videos ----------------------------------------------------------------


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    root = tmp_path_factory.mktemp("watermark-videos")
    cache: dict[str, Path] = {}

    def make(path: Path, size: str, seconds: float, audio: bool, extra=()) -> Path:
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c=black:size={size}:rate=30"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        args += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p"]
        args += ["-c:a", "aac"] if audio else ["-an"]
        subprocess.run([*args, *extra, str(path)], check=True, timeout=600)
        return path

    def get(name: str) -> Path:
        if name in cache:
            return cache[name]
        folder = root / name
        folder.mkdir()
        if name == "landscape":
            path = make(folder / "landscape.mp4", "1280x720", 2, True)
        elif name == "portrait":
            path = make(folder / "portrait.mp4", "720x1280", 2, True)
        elif name == "silent":
            path = make(folder / "silent.mp4", "640x480", 2, False)
        elif name == "uhd":
            path = make(folder / "uhd.mp4", "3840x2160", 2, True)
        elif name == "rotated":
            plain = get("landscape")
            path = folder / "IMG_7000.MOV"
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-display_rotation:v:0", "90", "-i", str(plain), "-c", "copy",
                            str(path)], check=True, timeout=120)
        else:
            raise AssertionError(name)
        cache[name] = path
        return path

    return get


@needs_ffmpeg
@needs_font
@pytest.mark.parametrize("name,dimensions,has_audio", [
    ("landscape", (1280, 720), True),
    ("portrait", (720, 1280), True),
    ("silent", (640, 480), False),
    ("uhd", (3840, 2160), True),
])
async def test_a_video_keeps_resolution_audio_and_duration(name, dimensions, has_audio, videos,
                                                           tmp_path):
    source = videos(name)
    before = probe(source)
    analysis = await service().analyze(source)

    result = await service().apply(source, workspace_in(tmp_path), analysis, WatermarkSpec("@luna"))

    after = probe(result.path)
    assert (after["width"], after["height"]) == dimensions        # never scaled
    assert after["video_codec"] == "h264"
    assert bool(after["audio_codecs"]) is has_audio               # silent stays silent
    assert after["duration"] == pytest.approx(before["duration"], abs=0.2)
    assert after["fps"] == pytest.approx(before["fps"], abs=0.1)


@needs_ffmpeg
@needs_font
async def test_the_watermark_is_there_from_start_to_finish(videos, tmp_path):
    source = videos("landscape")     # black frames: any ink is the watermark
    analysis = await service().analyze(source)
    result = await service().apply(source, workspace_in(tmp_path), analysis,
                                   WatermarkSpec("@lunaaa", position=Position.BOTTOM_RIGHT))

    corner = (1280 // 2, 720 // 2, 1280, 720)
    opposite = (0, 0, 1280 // 2, 720 // 2)
    for moment in (0.1, 1.0, 1.9):
        frame = frame_at(result.path, moment, tmp_path / f"f{moment}.png")
        assert brightness(frame, corner) > 0.5, f"no watermark at {moment}s"
        assert brightness(frame, opposite) < 0.05, f"ink in the wrong corner at {moment}s"


@needs_ffmpeg
@needs_font
async def test_a_rotated_video_is_watermarked_upright(videos, tmp_path):
    source = videos("rotated")
    stored = probe(source)
    assert (stored["width"], stored["height"]) == (1280, 720) and stored["rotation"] in (90, -90)

    analysis = await service().analyze(source)
    assert (analysis.width, analysis.height) == (720, 1280)
    result = await service().apply(source, workspace_in(tmp_path), analysis, WatermarkSpec("@luna"))

    after = probe(result.path)
    assert (after["width"], after["height"]) == (720, 1280)
    assert after["rotation"] == 0


@needs_ffmpeg
@needs_font
async def test_the_4k_encode_uses_the_memory_safe_settings(videos, tmp_path, monkeypatch):
    """The same bounded threads and lookahead the optimizer needs on Railway."""
    source = videos("uhd")
    analysis = await service().analyze(source)
    plan = plan_video(analysis, WatermarkSpec("@luna"), resolve_font())
    args = watermark_media.build_video_args("ffmpeg", source, tmp_path / "o.mp4", analysis,
                                            plan, WatermarkSpec("@luna"))
    assert plan.threads == 2
    # A 4K frame gets the light encoder; the quality one needs ~1 GB here.
    assert (plan.x264_preset, plan.lookahead) == ("ultrafast", 0)
    assert args[args.index("-rc-lookahead") + 1] == "0"
    assert args[args.index("-filter_threads") + 1] == "2"
    assert args.index("-threads") < args.index("-i")      # the decoder is bounded too

    peaks = []
    real = watermark_media.run_command_streaming

    async def measured(arguments, **kwargs):
        arguments = list(arguments)
        arguments.insert(1, "-benchmark")
        arguments[arguments.index("-loglevel") + 1] = "info"
        result = await real(arguments, **kwargs)
        peaks.append(int(re.search(r"maxrss=(\d+)", result.stderr).group(1)) / 1024)
        return result

    monkeypatch.setattr(watermark_media, "run_command_streaming", measured)
    await service().apply(source, workspace_in(tmp_path), analysis, WatermarkSpec("@luna"))
    assert peaks and max(peaks) < 500, peaks


# --- layout and text (pure) -------------------------------------------------------


def test_the_font_scales_with_the_frame_not_the_pixels():
    spec = WatermarkSpec("@luna", size=Size.M)
    small = plan_layout(640, 360, spec, resolve_font())
    large = plan_layout(3840, 2160, spec, resolve_font())
    assert large.font_size > small.font_size * 5
    assert small.font_size >= watermark_media.MIN_FONT_SIZE
    assert large.padding > small.padding


def test_a_long_line_is_shrunk_until_it_fits():
    # A measurer where every character is 10 px wide at size 100.
    def measure(size: int) -> float:
        return 40 * size / 10

    fitted = fit_font_size(width=200, height=1000, ratio=0.05, padding=10, measure=measure)
    assert measure(fitted) <= 180
    assert fitted < 50           # the nominal size for a 1000 px frame


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@lunaaa", "@lunaaa"),
        ("  t.me/model  ", "t.me/model"),
        ("Model Name", "Model Name"),
        ("first\nsecond", "first second"),
        ("a" * MAX_TEXT_LENGTH, "a" * MAX_TEXT_LENGTH),
        ("a" * (MAX_TEXT_LENGTH + 1), None),
        ("   ", None),
        ("...", None),
        ("", None),
        (None, None),
    ],
)
def test_watermark_text_is_one_tidy_line(raw, expected):
    assert clean_watermark_text(raw) == expected


def test_invisible_and_direction_tricks_are_stripped():
    assert clean_watermark_text("we​ird") == "weird"
    assert clean_watermark_text("‮evil") == "evil"


def test_drawtext_reads_its_text_and_font_from_the_workspace():
    spec = WatermarkSpec("@luna: a/b", position=Position.BOTTOM_CENTER, opacity=50)
    filter_string = build_drawtext(spec, plan_layout(1920, 1080, spec, resolve_font()))
    # The text never reaches the filter graph, so its colons cannot break it.
    assert "@luna" not in filter_string
    assert "textfile=watermark.txt" in filter_string
    assert "fontfile=watermark_font.ttf" in filter_string
    assert "fontcolor=white@0.50" in filter_string
    assert "x=(w-text_w)/2" in filter_string


def test_styles_map_to_colour_and_shadow():
    plain = WatermarkSpec("@x", style=Style.WHITE)
    black = WatermarkSpec("@x", style=Style.BLACK)
    shadowed = WatermarkSpec("@x", style=Style.WHITE_SHADOW)
    layout = plan_layout(1920, 1080, plain, resolve_font())
    assert "shadow" not in build_drawtext(plain, layout)
    assert "fontcolor=black@" in build_drawtext(black, layout)
    assert "shadowcolor=black@" in build_drawtext(shadowed, layout)


@pytest.mark.parametrize(
    "original,kind,fmt,expected",
    [
        ("Holiday Clip.MOV", MediaKind.VIDEO, None, "atreox_watermarked_Holiday_Clip.mp4"),
        ("logo.png", MediaKind.IMAGE, "png", "atreox_watermarked_logo.png"),
        (None, MediaKind.IMAGE, "jpeg", "atreox_watermarked_photo.jpg"),
    ],
)
def test_output_names(original, kind, fmt, expected):
    assert watermarked_filename(original, kind, fmt) == expected


def test_a_missing_font_is_a_clear_failure():
    with pytest.raises(MediaProcessingError) as excinfo:
        resolve_font(None, candidates=("/nowhere/none.ttf",), exists=lambda _: False)
    assert excinfo.value.code is ProcessingErrorCode.TOOL_MISSING
    assert watermark_router._failure_text(excinfo.value) == texts.WATERMARK_FONT_MISSING


def test_a_configured_font_wins():
    assert resolve_font("/custom/brand.ttf", exists=lambda path: path == "/custom/brand.ttf") \
        == "/custom/brand.ttf"


# --- presets (database) ------------------------------------------------------------


def spec_values(**overrides) -> dict:
    values = dict(position=Position.BOTTOM_RIGHT.value, style=Style.WHITE_SHADOW.value,
                  size=Size.M.value, opacity=75)
    values.update(overrides)
    return values


async def test_presets_are_created_listed_and_scoped_to_their_owner(session):
    repository = WatermarkPresetsRepository(session)
    await repository.create(1, name="@lunaaa", text="@lunaaa", **spec_values())
    await repository.create(1, name="t.me/luna", text="t.me/luna", **spec_values(size="l"))
    await repository.create(2, name="@mia", text="@mia", **spec_values())

    mine = await repository.list_for(1)
    assert [preset.name for preset in mine] == ["@lunaaa", "t.me/luna"]
    assert await repository.count_for(1) == 2
    # Another user's preset is invisible and unreachable, even by id.
    theirs = await repository.list_for(2)
    assert [preset.name for preset in theirs] == ["@mia"]
    assert await repository.get(theirs[0].id, 1) is None
    assert await repository.get(theirs[0].id, 2) is not None


async def test_presets_can_be_renamed_retexted_and_deleted(session):
    repository = WatermarkPresetsRepository(session)
    preset = await repository.create(7, name="@old", text="@old", **spec_values())

    await repository.rename(preset, "@new")
    await repository.set_text(preset, "t.me/new")
    await repository.set_look(preset, position=Position.TOP_LEFT.value,
                              style=Style.BLACK.value, size=Size.S.value, opacity=25)
    reloaded = await repository.get(preset.id, 7)
    assert (reloaded.name, reloaded.text) == ("@new", "t.me/new")
    assert (reloaded.position, reloaded.style, reloaded.size, reloaded.opacity) == \
        ("top_left", "black", "s", 25)

    await repository.delete(reloaded)
    assert await repository.list_for(7) == []


async def test_a_user_may_keep_ten_presets(session):
    repository = WatermarkPresetsRepository(session)
    for index in range(MAX_PRESETS_PER_USER):
        await repository.create(5, name=f"@handle{index}", text=f"@handle{index}", **spec_values())
    with pytest.raises(PresetLimitReached):
        await repository.create(5, name="@toomany", text="@toomany", **spec_values())
    # The cap is per user: someone else is unaffected.
    await repository.create(6, name="@handle0", text="@handle0", **spec_values())


async def test_names_are_unique_per_user(session):
    repository = WatermarkPresetsRepository(session)
    await repository.create(9, name="@luna", text="@luna", **spec_values())
    with pytest.raises(DuplicateName):
        await repository.create(9, name="@luna", text="@luna2", **spec_values())
    second = await repository.create(9, name="@other", text="@other", **spec_values())
    with pytest.raises(DuplicateName):
        await repository.rename(second, "@luna")


# --- the flow, through the router --------------------------------------------------


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(watermark_router, "Message", FakeMessage)


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(watermark_router, "JobsRepository", lambda session: recorder)
    return recorder


@pytest.fixture
def file_server(monkeypatch):
    server = FakeFileServer()
    monkeypatch.setattr(telegram_files, "AiohttpFileClient", lambda: server)
    return server


def as_document(path: Path, mime: str, *, size: int | None = None) -> FakeMessage:
    return FakeMessage(document=SimpleNamespace(
        file_id="DOC-1", file_size=size or path.stat().st_size, file_name=path.name,
        mime_type=mime))


async def walk_to_confirmation(message: FakeMessage, context: FSMContext, settings, *,
                               text: str = "@lunaaa") -> None:
    """Menu -> file -> text -> position -> style -> size -> opacity."""
    await watermark_router.handle_media(message, context, settings=settings)
    await watermark_router.ask_for_text(FakeCallback(message), context)
    await watermark_router.handle_text(FakeMessage(text=text), context, settings=settings)
    for handler, action, value in (
        (watermark_router.handle_position, "pos", Position.BOTTOM_RIGHT.value),
        (watermark_router.handle_style, "style", Style.WHITE_SHADOW.value),
        (watermark_router.handle_size, "size", Size.M.value),
        (watermark_router.handle_opacity, "opacity", "75"),
    ):
        await handler(FakeCallback(message), WatermarkCallback(action=action, value=value), context)


@needs_ffmpeg
@needs_font
async def test_the_whole_flow_from_menu_to_watermarked_file(photos, tmp_path, clean_env, jobs,
                                                            file_server, session):
    source = photos("landscape_jpeg")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = WizardMessage(keep=tmp_path / "received")
    message.document = SimpleNamespace(file_id="DOC-1", file_size=source.stat().st_size,
                                       file_name=source.name, mime_type="image/jpeg")

    await watermark_router.open_watermark(FakeCallback(message), context, session=session)
    assert message.edits[-1] == texts.WATERMARK_PROMPT
    assert await context.get_state() == WatermarkStates.waiting_for_media.state

    await walk_to_confirmation(message, context, settings)
    assert message.edits[-1] == texts.watermark_summary(
        text="@lunaaa", position="bottom_right", style="white_shadow", size="m", opacity=75)
    assert "Apply?" in message.edits[-1]
    assert await context.get_state() == WatermarkStates.confirming.state

    await watermark_router.apply_watermark(
        FakeCallback(message), context, bot=FakeBot(SERVER_FILE, size=source.stat().st_size),
        settings=settings, session=None,
    )

    (document,) = message.documents
    assert document["filename"] == "atreox_watermarked_landscape.jpg"
    assert document["no_detection"] is True
    with Image.open(document["copy"]) as after, Image.open(source) as before:
        assert after.size == before.size
    assert message.answers[-1] == texts.WATERMARK_DONE
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == ["🖼 Watermark Another", "💾 My Presets", "🏠 Main Menu"]
    assert jobs.created[0]["job_type"] is JobType.WATERMARK
    assert jobs.status == "success"
    assert await context.get_state() is None
    assert workspaces_left(settings) == []
    assert file_server.deleted == file_server.requested


@needs_ffmpeg
@needs_font
async def test_a_large_video_takes_the_local_bot_api_path(videos, tmp_path, clean_env, jobs,
                                                          file_server):
    source = videos("landscape")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = FakeMessage(keep=tmp_path / "received")
    # Declared far above the old 20 MB cloud limit.
    message.document = SimpleNamespace(file_id="BIG", file_size=300 * MB,
                                       file_name="clip.mp4", mime_type="video/mp4")
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await walk_to_confirmation(message, context, settings)
    await watermark_router.apply_watermark(FakeCallback(message), context, bot=bot,
                                           settings=settings, session=None)

    assert jobs.status == "success", message.answers
    (document,) = message.documents
    assert document["probe"]["video_codec"] == "h264"
    assert file_server.requested and all(
        url.startswith("http://telegram-bot-api.railway.internal:8082/")
        for url in file_server.requested
    )
    assert bot.downloads == []          # never the cloud download path
    assert workspaces_left(settings) == []


async def test_a_preset_can_be_used_and_saved(session, tmp_path, clean_env, jobs):
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = WizardMessage()
    message.document = SimpleNamespace(file_id="D", file_size=1000, file_name="a.jpg",
                                       mime_type="image/jpeg")
    repository = WatermarkPresetsRepository(session)
    preset = await repository.create(42, name="@lunaaa", text="@lunaaa",
                                     **spec_values(position="top_left", size="l", opacity=50))

    await watermark_router.handle_media(message, context, settings=settings)
    await watermark_router.show_presets(FakeCallback(message), session=session)
    assert message.edits[-1] == texts.WATERMARK_PRESETS_TITLE
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == ["💾 @lunaaa", "➕ Save New", "← Back"]

    await watermark_router.show_preset(
        FakeCallback(message), WatermarkCallback(action="detail", value=str(preset.id)),
        context, session=session)
    assert "@lunaaa" in message.edits[-1] and "Top Left" in message.edits[-1]
    assert texts.BTN_WATERMARK_USE in [
        b.text for row in message.markups[-1].inline_keyboard for b in row]

    await watermark_router.use_preset(
        FakeCallback(message), WatermarkCallback(action="use", value=str(preset.id)),
        context, session=session)
    assert await context.get_state() == WatermarkStates.confirming.state
    assert message.edits[-1] == texts.watermark_summary(
        text="@lunaaa", position="top_left", style="white_shadow", size="l", opacity=50)


async def test_saving_the_current_watermark_as_a_preset(session, tmp_path, clean_env):
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = FakeMessage()
    message.document = SimpleNamespace(file_id="D", file_size=1000, file_name="a.jpg",
                                       mime_type="image/jpeg")
    await walk_to_confirmation(message, context, settings, text="t.me/lunaaa")

    callback = FakeCallback(message)
    await watermark_router.save_preset(callback, context, session=session)

    assert callback.answers == [texts.WATERMARK_PRESET_SAVED]
    presets = await WatermarkPresetsRepository(session).list_for(42)
    assert [(p.name, p.text, p.position, p.opacity) for p in presets] == [
        ("t.me/lunaaa", "t.me/lunaaa", "bottom_right", 75)]

    # Saving the same one twice is refused, not duplicated.
    second = FakeCallback(message)
    await watermark_router.save_preset(second, context, session=session)
    assert second.answers == [texts.WATERMARK_PRESET_DUPLICATE]
    assert await WatermarkPresetsRepository(session).count_for(42) == 1


async def test_presets_can_be_managed_from_the_bot(session, tmp_path, clean_env):
    context = fsm_for()
    message = WizardMessage()
    repository = WatermarkPresetsRepository(session)
    preset = await repository.create(42, name="@old", text="@old", **spec_values())

    # Rename.
    await watermark_router.ask_for_rename(
        FakeCallback(message), WatermarkCallback(action="rename", value=str(preset.id)), context)
    assert await context.get_state() == WatermarkStates.renaming_preset.state
    await watermark_router.handle_preset_edit(FakeMessage(text="@new"), context, session=session)
    assert (await repository.get(preset.id, 42)).name == "@new"

    # Edit the text it draws.
    await watermark_router.ask_for_new_text(
        FakeCallback(message), WatermarkCallback(action="edit", value=str(preset.id)), context)
    await watermark_router.handle_preset_edit(FakeMessage(text="t.me/new"), context, session=session)
    assert (await repository.get(preset.id, 42)).text == "t.me/new"

    # Delete, with a confirmation step.
    await watermark_router.confirm_delete(
        FakeCallback(message), WatermarkCallback(action="delete", value=str(preset.id)),
        session=session)
    assert "@new" in message.edits[-1]
    callback = FakeCallback(message)
    await watermark_router.delete_preset(
        callback, WatermarkCallback(action="delete_yes", value=str(preset.id)), session=session)
    assert callback.answers[0] == texts.WATERMARK_PRESET_DELETED
    assert await repository.list_for(42) == []
    assert message.edits[-1] == texts.WATERMARK_PRESETS_EMPTY


async def test_a_new_preset_can_be_saved_from_the_list(session, tmp_path, clean_env):
    context = fsm_for()
    message = FakeMessage()
    await watermark_router.ask_for_new_preset(FakeCallback(message), context)
    assert await context.get_state() == WatermarkStates.typing_new_preset.state

    await watermark_router.handle_new_preset(FakeMessage(text="@fresh"), context, session=session)

    presets = await WatermarkPresetsRepository(session).list_for(42)
    assert [p.text for p in presets] == ["@fresh"]
    assert presets[0].position == "bottom_right" and presets[0].opacity == 75


async def test_one_user_cannot_reach_another_users_preset(session, tmp_path, clean_env):
    repository = WatermarkPresetsRepository(session)
    theirs = await repository.create(99, name="@theirs", text="@theirs", **spec_values())
    message = WizardMessage()        # acting as user 42
    callback = FakeCallback(message)

    await watermark_router.show_preset(
        callback, WatermarkCallback(action="detail", value=str(theirs.id)), fsm_for(),
        session=session)

    assert callback.answers == [texts.WATERMARK_PRESET_GONE]
    assert message.edits == []


async def test_the_preset_limit_is_explained(session, tmp_path, clean_env):
    repository = WatermarkPresetsRepository(session)
    for index in range(MAX_PRESETS_PER_USER):
        await repository.create(42, name=f"@h{index}", text=f"@h{index}", **spec_values())
    context = fsm_for()
    message = FakeMessage()
    await watermark_router.ask_for_new_preset(FakeCallback(message), context)

    typed = FakeMessage(text="@eleven")
    await watermark_router.handle_new_preset(typed, context, session=session)

    assert typed.answers[-1] == texts.watermark_preset_limit(MAX_PRESETS_PER_USER)
    assert await repository.count_for(42) == MAX_PRESETS_PER_USER


# --- guards and failures -----------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        FakeMessage(text="hello"),
        FakeMessage(document=SimpleNamespace(file_id="D", file_size=10, file_name="a.pdf",
                                             mime_type="application/pdf")),
        FakeMessage(sticker=SimpleNamespace(file_id="S")),
    ],
    ids=["text", "pdf", "sticker"],
)
async def test_unsupported_input_is_explained(message, tmp_path, clean_env):
    context = fsm_for()
    await context.set_state(WatermarkStates.waiting_for_media)
    await watermark_router.handle_media(message, context, settings=local_settings(tmp_path))
    assert message.answers == [texts.WATERMARK_UNSUPPORTED]
    assert await context.get_state() == WatermarkStates.waiting_for_media.state


async def test_a_too_long_watermark_is_rejected(tmp_path, clean_env):
    context = fsm_for()
    await context.set_state(WatermarkStates.typing_text)
    message = FakeMessage(text="x" * 200)
    await watermark_router.handle_text(message, context, settings=local_settings(tmp_path))
    assert message.answers == [texts.WATERMARK_TEXT_REJECTED]
    # Still waiting for a usable line.
    assert await context.get_state() == WatermarkStates.typing_text.state


async def test_an_oversized_file_is_refused_before_fetching(tmp_path, clean_env):
    context = fsm_for()
    await context.set_state(WatermarkStates.waiting_for_media)
    message = as_document(Path("x.mp4"), "video/mp4", size=2001 * MB)
    await watermark_router.handle_media(message, context, settings=local_settings(tmp_path))
    assert message.answers == [texts.ERROR_TOO_LARGE]


@needs_ffmpeg
@needs_font
async def test_cleanup_after_a_failed_render(photos, tmp_path, clean_env, jobs, file_server,
                                             monkeypatch):
    source = photos("png")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = FakeMessage()
    message.document = SimpleNamespace(file_id="D", file_size=source.stat().st_size,
                                       file_name=source.name, mime_type="image/png")
    await walk_to_confirmation(message, context, settings)

    async def broken(*args, **kwargs):
        raise MediaProcessingError(ProcessingErrorCode.ENCODE_FAILED, "boom")

    monkeypatch.setattr(WatermarkService, "apply", broken)

    await watermark_router.apply_watermark(
        FakeCallback(message), context, bot=FakeBot(SERVER_FILE, size=source.stat().st_size),
        settings=settings, session=None)

    assert message.documents == []
    assert message.answers[-1] == texts.ERROR_PROCESSING
    assert jobs.status == "failed" and jobs.error_code == "encode_failed"
    assert workspaces_left(settings) == []
    # The confirmation is still on screen, so Apply can be tapped again.
    assert await context.get_state() == WatermarkStates.confirming.state
    for answer in message.answers:
        assert str(settings.temp_root) not in answer


async def test_cancel_and_stale_taps(tmp_path, clean_env):
    context = fsm_for()
    await context.set_state(WatermarkStates.confirming)
    message = FakeMessage()
    callback = FakeCallback(message)
    await watermark_router.cancel(callback, context)
    assert await context.get_state() is None
    assert callback.answers == [texts.CANCELLED]

    stale = FakeCallback(message)
    await watermark_router.stale_apply(stale)
    assert stale.answers == [texts.WATERMARK_CHOICE_EXPIRED]


# --- menu, help, analytics ----------------------------------------------------------


def test_watermark_is_in_the_main_menu():
    buttons = [b for row in main_menu().inline_keyboard for b in row]
    labels = [b.text for b in buttons]
    assert labels.index("🖼 Watermark") == labels.index("🗜 Media Optimizer") + 1
    assert buttons[labels.index("🖼 Watermark")].callback_data == \
        MenuCallback(action="watermark").pack()


def test_the_prompt_is_the_specified_copy():
    assert texts.WATERMARK_PROMPT == (
        "🖼 Send me a photo or video.\n\n"
        "I'll add your @username or custom text as a clean watermark.\n\n"
        "For best quality, send media as a File."
    )


def test_help_describes_the_watermark_tool():
    assert ("🖼 <b>Watermark</b>\nAdds your @username or custom branding text to photos and "
            "videos. Save presets for repeated use.") in texts.HELP


def test_the_summary_matches_the_specified_shape():
    assert texts.watermark_summary(text="@username", position="bottom_right",
                                   style="white_shadow", size="m", opacity=75) == (
        "🖼 <b>Watermark</b>\n\n"
        "Text: @username\n"
        "Position: Bottom Right\n"
        "Style: White + Shadow\n"
        "Size: M\n"
        "Opacity: 75%\n\n"
        "Apply?"
    )


def test_the_keyboards_cover_every_choice():
    from app.bot.keyboards.watermark import (
        confirmation_choices, opacity_choices, position_choices, size_choices, style_choices,
    )

    positions = [b.text for row in position_choices().inline_keyboard for b in row]
    assert positions == ["↖️ Top Left", "↗️ Top Right", "↙️ Bottom Left", "↘️ Bottom Right",
                         "⬇️ Bottom Center", "❌ Cancel"]
    styles = [b.text for row in style_choices().inline_keyboard for b in row]
    assert styles == ["⚪ White", "⚫ Black", "✨ White + Shadow", "❌ Cancel"]
    sizes = [b.text for row in size_choices().inline_keyboard for b in row]
    assert sizes == ["S", "M", "L", "❌ Cancel"]
    opacities = [b.text for row in opacity_choices(OPACITIES).inline_keyboard for b in row]
    assert opacities == ["25%", "50%", "75%", "100%", "❌ Cancel"]
    confirm = [b.text for row in confirmation_choices().inline_keyboard for b in row]
    assert confirm == ["✅ Apply", "💾 Save as Preset", "✏️ Change", "❌ Cancel"]


def test_every_callback_payload_fits_telegrams_budget():
    preset = SimpleNamespace(id=999999, name="@averylonghandle_that_is_long")
    for markup in (preset_list([preset]), preset_detail(999999, can_use=True), watermark_done()):
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode("utf-8")) <= 64


async def test_opening_the_tool_records_the_feature(session):
    await watermark_router.open_watermark(FakeCallback(FakeMessage()), fsm_for(), session=session)
    count = await session.scalar(select(func.count()).select_from(FeatureEvent)
                                 .where(FeatureEvent.feature == "watermark"))
    assert count == 1


async def test_watermarks_are_counted_in_stats(session):
    repository = JobsRepository(session)
    for job_type in (JobType.WATERMARK, JobType.WATERMARK, JobType.CIRCLE):
        job = await repository.create(job_id=uuid.uuid4(), telegram_user_id=1, job_type=job_type)
        await repository.mark_success(job)
    failed = await repository.create(job_id=uuid.uuid4(), telegram_user_id=1,
                                     job_type=JobType.WATERMARK)
    await repository.mark_failed(failed, error_code="encode_failed")

    stats = await StatsRepository(session).collect()
    assert stats.watermarks == 2
    report = texts.stats_report(stats)
    assert "🖼 Watermarks: 2" in report
    assert report.index("🗜 Optimizations") < report.index("🖼 Watermarks")


def test_the_preset_table_columns_fit_their_limits():
    assert JobType.WATERMARK.value == "watermark"
    assert Feature.WATERMARK.value == "watermark"
    assert MAX_PRESETS_PER_USER == 10
    columns = WatermarkPreset.__table__.columns
    assert columns["name"].type.length == columns["text"].type.length == 64
    assert MAX_TEXT_LENGTH <= 64

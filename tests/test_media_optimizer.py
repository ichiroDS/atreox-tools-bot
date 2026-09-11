"""Phase 8: Media Optimizer.

Real media throughout: FFmpeg generates the videos (with noise, so rate
control behaves as it does on camera footage), Pillow the photos, and every
output is checked with ffprobe - and, for photos, pixel by pixel - rather than
trusted. Telegram is faked, the tools never are; tests that need a tool skip
without it.
"""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, GetFile, SendDocument
from aiogram.types import Message
from sqlalchemy import func, select

from app.bot import texts
from app.bot.callbacks import MenuCallback, OptimizeCallback
from app.bot.keyboards.common import main_menu
from app.bot.keyboards.optimizer import optimize_done, preset_choices
from app.bot.routers import optimizer as optimizer_router
from app.bot.states import OptimizerStates
from app.db.models import Feature, FeatureEvent, JobType
from app.db.repositories import JobsRepository, StatsRepository
from app.services import telegram_files
from app.services.jobgate import MediaJobGate
from app.services.media import image_worker
from app.services.media import optimizer as optimizer_media
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.optimizer import (
    ImageAnalysis,
    MediaKind,
    MediaOptimizerService,
    Preset,
    VideoAnalysis,
    build_video_args,
    classify_failure,
    is_worth_sending,
    optimized_filename,
    output_size,
    parse_analysis,
    parse_rate,
    plan_all,
    plan_video,
)
from app.services.telegram_files import FileKind, IncomingFile, extract_optimizer_source
from app.utils.temp_files import JobWorkspace
from tests.test_large_files import (
    MB,
    SERVER_FILE,
    TOKEN,
    FakeBot,
    FakeFileServer,
    FakeJobs,
    local_settings,
    workspaces_left,
)

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageChops, ImageCms  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


# --- ffprobe and friends: the tests' own source of truth ---------------------


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         "--", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    videos = [s for s in data["streams"] if s["codec_type"] == "video"]
    audios = [s for s in data["streams"] if s["codec_type"] == "audio"]
    video = videos[0] if videos else {}
    rotation = 0
    for side in video.get("side_data_list") or []:
        rotation = int(side.get("rotation", rotation) or 0)
    return {
        "format": data["format"]["format_name"],
        "duration": float(data["format"].get("duration") or 0),
        "tags": {k.lower(): v for k, v in (data["format"].get("tags") or {}).items()},
        "video_codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "pix_fmt": video.get("pix_fmt"),
        "fps": parse_rate(video.get("avg_frame_rate")),
        "rotation": rotation,
        "videos": len(videos),
        "audio_codecs": [a["codec_name"] for a in audios],
        "audio_channels": [a.get("channels") for a in audios],
    }


def top_level_boxes(path: Path) -> list[str]:
    """MP4 top-level box order, read from the headers only."""
    boxes = []
    with open(path, "rb") as handle:
        while True:
            header = handle.read(8)
            if len(header) < 8:
                break
            size, kind = int.from_bytes(header[:4], "big"), header[4:8].decode("latin-1")
            boxes.append(kind)
            if size == 1:
                size = int.from_bytes(handle.read(8), "big")
                handle.seek(size - 16, 1)
            elif size == 0:
                break
            else:
                handle.seek(size - 8, 1)
    return boxes


def decoded_md5(path: Path) -> str:
    """Hash of the decoded pixels, independent of how they were compressed."""
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "md5", "-"],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def same_pixels(a: Path, b: Path) -> bool:
    with Image.open(a) as first, Image.open(b) as second:
        mode = "RGBA" if "A" in first.getbands() or "transparency" in first.info else "RGB"
        return ImageChops.difference(first.convert(mode), second.convert(mode)).getbbox() is None


# --- real media ---------------------------------------------------------------


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, timeout=600)


def make_video(path: Path, *, size: str, seconds: float, rate: int = 30, bitrate: str = "8M",
               audio: bool = True, channels: int = 1, container_args=(), video_codec="libx264",
               audio_codec="aac", metadata=()) -> Path:
    args = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    args += ["-t", str(seconds), "-vf", "noise=alls=20:allf=t", "-c:v", video_codec,
             "-b:v", bitrate, "-pix_fmt", "yuv420p"]
    if video_codec == "libx264":
        args += ["-preset", "ultrafast"]
    if video_codec == "libvpx":
        args += ["-deadline", "realtime", "-cpu-used", "8"]
    if audio:
        args += ["-c:a", audio_codec, "-ac", str(channels)]
    else:
        args += ["-an"]
    for key, value in metadata:
        args += ["-metadata", f"{key}={value}"]
    _ffmpeg(*args, *container_args, str(path))
    return path


VIDEO_RECIPES = {
    "4k": dict(size="3840x2160", seconds=2, bitrate="40M"),
    "1080p": dict(size="1920x1080", seconds=3, bitrate="14M"),
    "720p": dict(size="1280x720", seconds=3, bitrate="6M"),
    "portrait": dict(size="1080x1920", seconds=3, bitrate="14M"),
    "square": dict(size="1440x1440", seconds=3, bitrate="12M"),
    "silent": dict(size="1280x720", seconds=3, bitrate="6M", audio=False),
    "surround": dict(size="640x360", seconds=3, bitrate="3M", channels=6),
    "slowmo": dict(size="640x360", seconds=2, rate=120, bitrate="6M"),
    "long": dict(size="426x240", seconds=150, rate=15, bitrate="1500k"),
    "tiny": dict(size="640x360", seconds=3, bitrate="60k"),
    "tagged": dict(size="640x360", seconds=2, bitrate="3M",
                   metadata=(("title", "Private title"), ("location", "+50.4501+030.5234/"),
                             ("comment", "shot on my phone"))),
}
VIDEO_EXTENSIONS = {"silent": ".mkv"}


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    """Real videos, generated once per module; each in its own folder, which
    doubles as the Bot API server's --dir when a test borrows it in place."""
    root = tmp_path_factory.mktemp("optimizer-videos")
    cache: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name not in cache:
            folder = root / name
            folder.mkdir()
            if name == "rotated":
                # A phone held upright: landscape pixels plus a 90° display matrix.
                plain = get("1080p")
                target = folder / "IMG_0001.MOV"
                _ffmpeg("-display_rotation:v:0", "90", "-i", str(plain), "-c", "copy", str(target))
            elif name == "webm":
                target = make_video(folder / "clip.webm", size="640x360", seconds=2,
                                    bitrate="2M", video_codec="libvpx", audio_codec="libopus")
            elif name == "mov":
                target = make_video(folder / "clip.mov", size="1280x720", seconds=2, bitrate="6M")
            else:
                extension = VIDEO_EXTENSIONS.get(name, ".mp4")
                target = make_video(folder / f"{name}{extension}", **VIDEO_RECIPES[name])
            cache[name] = target
        return cache[name]

    return get


def _photo(width: int, height: int) -> Image.Image:
    """Noisy enough to behave like a camera photo under JPEG."""
    grey = Image.effect_noise((width, height), 28)
    gradient = Image.linear_gradient("L").resize((width, height))
    return Image.merge("RGB", (grey, gradient, Image.blend(grey, gradient, 0.5)))


def _orientation_exif(orientation: int = 6) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "Samsung"        # Make
    exif[0x0110] = "SM-G991B"       # Model
    exif[0x8825] = {1: "N", 2: (50.0, 27.0, 0.36), 3: "E", 4: (30.0, 31.0, 24.24)}  # GPS
    return exif.tobytes()


@pytest.fixture(scope="module")
def images(tmp_path_factory):
    root = tmp_path_factory.mktemp("optimizer-images")
    cache: dict[str, Path] = {}
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

    def get(name: str) -> Path:
        if name in cache:
            return cache[name]
        folder = root / name
        folder.mkdir()
        if name == "large_jpeg":
            path = folder / "DSC_0042.JPG"
            _photo(4000, 3000).save(path, quality=95, icc_profile=srgb)
        elif name == "portrait_jpeg":
            path = folder / "portrait.jpg"
            _photo(1500, 2000).save(path, quality=93)
        elif name == "landscape_jpeg":
            path = folder / "landscape.jpeg"
            _photo(2000, 1500).save(path, quality=93)
        elif name == "exif_jpeg":
            # Stored landscape, displayed portrait, with a GPS fix and device tags.
            path = folder / "IMG_1234.jpg"
            _photo(1600, 1200).save(path, quality=92, exif=_orientation_exif(6), icc_profile=srgb)
        elif name == "png":
            path = folder / "screenshot.png"
            _photo(1200, 900).save(path, compress_level=1)
        elif name == "transparent_png":
            path = folder / "logo.png"
            picture = _photo(800, 600).convert("RGBA")
            picture.putalpha(Image.linear_gradient("L").resize((800, 600)))
            picture.save(path, compress_level=1)
        elif name == "flat_png":
            path = folder / "diagram.png"
            picture = Image.new("RGB", (1000, 700), "white")
            for index, colour in enumerate(("red", "green", "blue", "black")):
                picture.paste(colour, (index * 200 + 50, 100, index * 200 + 200, 600))
            picture.save(path, compress_level=0)
        elif name == "palette_png":
            path = folder / "icon.png"
            picture = Image.new("P", (400, 400), 0)
            picture.putpalette([255, 255, 255, 255, 0, 0, 0, 0, 255] + [0] * 759)
            picture.paste(1, (50, 50, 350, 200))
            picture.paste(2, (50, 220, 350, 350))
            picture.save(path, transparency=0, compress_level=0)
        elif name == "deep_png":
            path = folder / "scan16.png"
            _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240", "-frames:v", "1",
                    "-pix_fmt", "rgb48be", "-compression_level", "0", str(path))
        elif name == "webp":
            path = folder / "photo.webp"
            _photo(1600, 1200).save(path, quality=95)
        elif name == "lossless_webp":
            path = folder / "art.webp"
            Image.linear_gradient("L").resize((600, 400)).convert("RGBA").save(
                path, lossless=True, quality=0, method=0)
        elif name == "animated_webp":
            path = folder / "sticker.webp"
            frames = [Image.new("RGB", (64, 64), c) for c in ("red", "green", "blue")]
            frames[0].save(path, save_all=True, append_images=frames[1:], duration=100)
        elif name == "optimized_png":
            # Already exactly what the optimizer itself would produce.
            source = get("flat_png")
            path = folder / "diagram.png"
            assert image_worker.main(["w", str(source), str(path), "png", "balanced"]) == 0
        else:
            raise AssertionError(name)
        cache[name] = path
        return path

    return get


# --- fakes ------------------------------------------------------------------


class FakeMessage:
    """Records what a handler sent; keeps a copy of every document it received."""

    def __init__(self, user_id: int = 42, *, keep: Path | None = None, parent=None, **media):
        self.chat = SimpleNamespace(id=user_id)
        self.from_user = SimpleNamespace(id=user_id, is_bot=False)
        for attribute in ("audio", "voice", "video", "video_note", "document", "photo",
                          "sticker", "animation", "text"):
            setattr(self, attribute, None)
        for attribute, value in media.items():
            setattr(self, attribute, value)
        self.keep = keep
        self.parent = parent
        self.answers: list[str] = []
        self.markups: list = []
        self.edits: list[str] = []
        self.documents: list[dict] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return FakeMessage(self.from_user.id, parent=self)

    async def answer_document(self, document=None, **kwargs):
        # The job deletes the file as soon as this returns: inspect it now.
        path = Path(document.path)
        record = {
            "filename": document.filename,
            "no_detection": kwargs.get("disable_content_type_detection"),
            "size": path.stat().st_size,
            "workspace": path.parent,
            "probe": probe(path),
        }
        if self.keep is not None:
            self.keep.mkdir(parents=True, exist_ok=True)
            record["copy"] = Path(shutil.copyfile(path, self.keep / f"{uuid.uuid4().hex}{path.suffix}"))
        self.documents.append(record)
        return SimpleNamespace(document=SimpleNamespace(file_name=document.filename))

    async def edit_text(self, text=None, **kwargs):
        (self.parent or self).edits.append(text)
        return self

    async def delete(self):
        return True


class FakeCallback:
    def __init__(self, message: FakeMessage, user_id: int = 42):
        self.message = message
        self.from_user = SimpleNamespace(id=user_id, is_bot=False)
        self.answers: list = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


def fsm_for(user_id: int = 42) -> FSMContext:
    return FSMContext(storage=MemoryStorage(),
                      key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))


@pytest_asyncio.fixture
async def state():
    context = fsm_for()
    await context.set_state(OptimizerStates.waiting_for_media)
    yield context
    await context.clear()


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(optimizer_router, "JobsRepository", lambda session: recorder)
    return recorder


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(optimizer_router, "Message", FakeMessage)


@pytest.fixture
def file_server(monkeypatch):
    server = FakeFileServer()
    monkeypatch.setattr(telegram_files, "AiohttpFileClient", lambda: server)
    return server


def incoming_for(path: Path, *, kind: FileKind, compressed=False, name: str | None = None):
    return IncomingFile(file_id=f"F-{path.stem}", kind=kind, size=path.stat().st_size,
                        original_filename=path.name if name is None else name,
                        compressed=compressed)


def kind_of(path: Path) -> FileKind:
    return FileKind.IMAGE if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") else FileKind.VIDEO


async def analyze_borrowed(tmp_path, source: Path, *, message=None, state=None, settings=None,
                           compressed=False, gate=None):
    settings = settings or local_settings(tmp_path, bot_api_local_dir=str(source.parent))
    message = message or FakeMessage()
    state = state or fsm_for()
    analysis = await optimizer_router._analyze(
        message=message, state=state, bot=FakeBot(str(source), size=source.stat().st_size),
        settings=settings, session=None,
        incoming=incoming_for(source, kind=kind_of(source), compressed=compressed),
        user_id=42, media_gate=gate,
    )
    return analysis, message, state


async def optimize_borrowed(tmp_path, source: Path, preset: Preset, *, message=None, state=None,
                            settings=None, gate=None, name: str | None = None):
    settings = settings or local_settings(tmp_path, bot_api_local_dir=str(source.parent))
    message = message or FakeMessage(keep=tmp_path / "received")
    outcome = await optimizer_router._run_optimize_job(
        message=message, state=state or fsm_for(), bot=FakeBot(str(source), size=source.stat().st_size),
        settings=settings, session=None, incoming=incoming_for(source, kind=kind_of(source), name=name),
        preset=preset, user_id=42, media_gate=gate,
    )
    return outcome, message, settings


def service(timeout: float = 300) -> MediaOptimizerService:
    return MediaOptimizerService(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe",
                                 timeout=timeout, probe_timeout=60)


def workspace_in(tmp_path: Path) -> JobWorkspace:
    path = tmp_path / f"ws-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return JobWorkspace(job_id=uuid.uuid4(), path=path)


def assert_nothing_leaked(message: FakeMessage, settings) -> None:
    for answer in message.answers:
        assert str(settings.temp_root) not in answer
        assert "Traceback" not in answer and "ffmpeg" not in answer.lower()


# --- planning (pure) --------------------------------------------------------


@pytest.mark.parametrize(
    "source,cap,expected",
    [
        ((3840, 2160), 720, (1280, 720)),
        ((3840, 2160), 1080, (1920, 1080)),
        ((1920, 1080), 720, (1280, 720)),
        ((1920, 1080), 1080, (1920, 1080)),
        ((1280, 720), 1080, (1280, 720)),       # never upscaled
        ((1080, 1920), 720, (720, 1280)),       # portrait: the short side is capped
        ((2160, 2160), 1080, (1080, 1080)),
        ((2560, 1080), 1080, (2560, 1080)),     # ultrawide 1080p is already 1080p
        ((1079, 1919), 1080, (1078, 1918)),     # odd sizes become even, never larger
        ((640, 360), 720, (640, 360)),
    ],
)
def test_output_size_caps_the_short_side_and_never_upscales(source, cap, expected):
    assert output_size(*source, cap) == expected


def analysis(**overrides) -> VideoAnalysis:
    values = dict(size_bytes=148 * MB, width=1920, height=1080, duration=102.0,
                  video_codec="h264", video_stream=0, frame_rate=30.0,
                  video_bitrate=11_500_000, bitrate_declared=True, audio_codec="aac",
                  audio_stream=1, audio_bitrate=192_000, audio_channels=2)
    values.update(overrides)
    return VideoAnalysis(**values)


def test_presets_are_ordered_and_adapt_to_the_source():
    plans = plan_all(analysis())
    small, balanced, high = plans[Preset.SMALL], plans[Preset.BALANCED], plans[Preset.HIGH]
    assert (small.width, small.height) == (1280, 720)
    assert (balanced.width, balanced.height) == (high.width, high.height) == (1920, 1080)
    assert small.estimated_bytes < balanced.estimated_bytes < high.estimated_bytes < 148 * MB
    assert all(plan.worthwhile for plan in plans.values())
    # The same preset spends more on a higher frame rate, less on a smaller picture.
    assert plan_video(analysis(frame_rate=60), Preset.BALANCED).video_bitrate > balanced.video_bitrate
    assert plan_video(analysis(width=1280, height=720), Preset.BALANCED).video_bitrate < balanced.video_bitrate


def test_a_preset_never_asks_for_more_bits_than_the_source_has():
    lean = analysis(video_bitrate=2_000_000, size_bytes=26 * MB)
    for plan in plan_all(lean).values():
        assert plan.video_bitrate <= 2_000_000


def test_an_efficient_source_is_not_offered_any_preset():
    lean = analysis(width=640, height=360, video_bitrate=60_000, size_bytes=900_000,
                    duration=102.0, audio_bitrate=64_000)
    assert not any(plan.worthwhile for plan in plan_all(lean).values())


def test_lean_aac_is_copied_and_rich_audio_is_re_encoded():
    assert plan_video(analysis(audio_bitrate=96_000), Preset.BALANCED).audio.copy is True
    rich = plan_video(analysis(audio_bitrate=320_000), Preset.SMALL).audio
    assert rich.copy is False and rich.bitrate == 96_000
    surround = plan_video(analysis(audio_channels=6), Preset.HIGH).audio
    assert surround.copy is False and surround.downmix is True
    assert plan_video(analysis(audio_codec=None, audio_stream=None), Preset.HIGH).audio is None


def test_slow_motion_frame_rates_are_capped_and_normal_ones_kept():
    assert plan_video(analysis(frame_rate=240), Preset.SMALL).frame_rate_cap == 60
    for fps in (24, 29.97, 30, 50, 60):
        assert plan_video(analysis(frame_rate=fps), Preset.SMALL).frame_rate_cap is None


def test_unknown_duration_means_no_estimate():
    assert plan_video(analysis(duration=0.0, video_bitrate=0), Preset.SMALL).estimated_bytes is None


def test_video_args_encode_h264_aac_faststart_without_metadata():
    source = analysis()
    args = build_video_args("ffmpeg", Path("/in.mov"), Path("/out.mp4"), source,
                            plan_video(source, Preset.BALANCED))
    pairs = dict(zip(args, args[1:]))
    assert pairs["-c:v"] == "libx264" and pairs["-profile:v"] == "high"
    assert pairs["-movflags"] == "+faststart"
    assert pairs["-map_metadata"] == "-1" and pairs["-map_chapters"] == "-1"
    assert pairs["-f"] == "mp4" and args[-1] == str(Path("/out.mp4"))
    assert "scale=1920:1080" in pairs["-vf"] and "format=yuv420p" in pairs["-vf"]
    assert {"-sn", "-dn"} <= set(args)
    silent = analysis(audio_codec=None, audio_stream=None)
    silent_args = build_video_args("ffmpeg", Path("/in"), Path("/out"), silent,
                                   plan_video(silent, Preset.SMALL))
    assert "-an" in silent_args and "-c:a" not in silent_args


@pytest.mark.parametrize(
    "original,kind,fmt,expected",
    [
        ("Holiday Clip.MOV", MediaKind.VIDEO, None, "atreox_optimized_Holiday_Clip.mp4"),
        ("clip.mkv", MediaKind.VIDEO, None, "atreox_optimized_clip.mp4"),
        (None, MediaKind.VIDEO, None, "atreox_optimized_video.mp4"),
        ("IMG_0001.JPEG", MediaKind.IMAGE, "jpeg", "atreox_optimized_IMG_0001.jpeg"),
        (None, MediaKind.IMAGE, "jpeg", "atreox_optimized_photo.jpg"),
        ("logo.png", MediaKind.IMAGE, "png", "atreox_optimized_logo.png"),
        ("../../etc/passwd", MediaKind.IMAGE, "webp", "atreox_optimized_passwd.webp"),
        ("відео.mp4", MediaKind.VIDEO, None, "atreox_optimized_video.mp4"),
    ],
)
def test_output_names_are_safe_and_carry_the_real_extension(original, kind, fmt, expected):
    assert optimized_filename(original, kind, fmt) == expected


def test_a_result_must_be_meaningfully_smaller_to_be_sent():
    assert is_worth_sending(100 * MB, 31 * MB)
    assert not is_worth_sending(100 * MB, 100 * MB)
    assert not is_worth_sending(100 * MB, 120 * MB)
    assert not is_worth_sending(100 * MB, 99 * MB)  # 1% is not worth a new file


@pytest.mark.parametrize(
    "stderr,code",
    [
        ("av_interleaved_write_frame(): No space left on device", "disk_full"),
        ("Error submitting packet to decoder: Invalid data found", "corrupt_input"),
        ("Unknown encoder 'libx265'", "encode_failed"),
    ],
)
def test_ffmpeg_failures_are_classified(stderr, code):
    assert classify_failure(stderr).value == code


@pytest.mark.parametrize("fmt", ["hls", "concat", "tty", "srt", "gif", "apng", ""])
def test_demuxers_outside_the_allowlist_are_refused(fmt):
    payload = json.dumps({"format": {"format_name": fmt},
                          "streams": [{"codec_type": "video", "codec_name": "h264",
                                       "width": 10, "height": 10}]})
    with pytest.raises(MediaProcessingError) as excinfo:
        parse_analysis(payload, 1000)
    assert excinfo.value.code is ProcessingErrorCode.UNSUPPORTED


def test_a_container_without_video_is_not_a_video():
    payload = json.dumps({"format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "3"},
                          "streams": [{"codec_type": "audio", "codec_name": "aac"}]})
    with pytest.raises(MediaProcessingError) as excinfo:
        parse_analysis(payload, 1000)
    assert excinfo.value.code is ProcessingErrorCode.UNSUPPORTED


def test_an_undeclared_bitrate_is_derived_from_size_and_duration():
    payload = json.dumps({
        "format": {"format_name": "matroska,webm", "duration": "10"},
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "vp9", "width": 1280,
                     "height": 720, "avg_frame_rate": "30000/1001"},
                    {"index": 1, "codec_type": "audio", "codec_name": "opus"}]})
    result = parse_analysis(payload, 10 * 1_000_000 // 8 * 5)  # 5 Mbit/s overall
    assert isinstance(result, VideoAnalysis)
    assert result.video_bitrate == 5_000_000 - 128_000
    assert result.bitrate_declared is False
    assert result.frame_rate == pytest.approx(29.97, abs=0.01)


@pytest.mark.parametrize("quality", [50, 75, 90, 95])
def test_jpeg_quality_is_read_back_from_the_file(quality, tmp_path):
    path = tmp_path / "q.jpg"
    _photo(64, 64).save(path, quality=quality)
    with Image.open(path) as picture:
        assert abs(image_worker.estimate_jpeg_quality(picture) - quality) <= 2


# --- input ------------------------------------------------------------------


def _doc(name: str, mime: str, size: int = 1000):
    return SimpleNamespace(file_id="D", file_size=size, file_name=name, mime_type=mime)


@pytest.mark.parametrize(
    "document,kind",
    [
        (_doc("a.jpg", "image/jpeg"), FileKind.IMAGE),
        (_doc("a.png", "image/png"), FileKind.IMAGE),
        (_doc("a.webp", "image/webp"), FileKind.IMAGE),
        (_doc("a.webp", "application/octet-stream"), FileKind.IMAGE),
        (_doc("a.mp4", "video/mp4"), FileKind.VIDEO),
        (_doc("a.mov", "video/quicktime"), FileKind.VIDEO),
        (_doc("a.webm", "video/webm"), FileKind.VIDEO),
        (_doc("a.mkv", "application/octet-stream"), FileKind.VIDEO),
        (_doc("a.avi", "video/x-msvideo"), FileKind.VIDEO),
    ],
)
def test_photo_and_video_files_are_accepted(document, kind):
    incoming = extract_optimizer_source(FakeMessage(document=document))
    assert incoming is not None and incoming.kind is kind and incoming.compressed is False


def test_native_photos_and_videos_are_accepted_and_marked_compressed():
    photo = extract_optimizer_source(FakeMessage(photo=[
        SimpleNamespace(file_id="S", file_size=10), SimpleNamespace(file_id="L", file_size=99)]))
    assert photo.file_id == "L" and photo.kind is FileKind.IMAGE and photo.compressed
    video = extract_optimizer_source(FakeMessage(video=SimpleNamespace(
        file_id="V", file_size=5, file_name=None, mime_type="video/mp4", duration=12)))
    assert video.kind is FileKind.VIDEO and video.duration == 12 and video.compressed


@pytest.mark.parametrize(
    "message",
    [
        FakeMessage(text="hi"),
        FakeMessage(document=_doc("a.heic", "image/heic")),
        FakeMessage(document=_doc("a.gif", "image/gif")),
        FakeMessage(document=_doc("a.pdf", "application/pdf")),
        FakeMessage(document=_doc("a.mp3", "audio/mpeg")),
        FakeMessage(sticker=SimpleNamespace(file_id="S")),
    ],
    ids=["text", "heic", "gif", "pdf", "audio", "sticker"],
)
async def test_other_input_is_explained_and_the_tool_stays_open(message, tmp_path, clean_env, state):
    bot = FakeBot(SERVER_FILE)
    await optimizer_router.handle_media(message, state, bot=bot, settings=local_settings(tmp_path),
                                        session=None)
    assert message.answers == [texts.OPTIMIZE_UNSUPPORTED]
    assert bot.downloads == []
    assert await state.get_state() == OptimizerStates.waiting_for_media.state


async def test_an_oversized_file_is_refused_before_fetching(tmp_path, clean_env, state, jobs):
    message = FakeMessage(document=_doc("huge.mov", "video/quicktime", size=2001 * MB))
    bot = FakeBot(SERVER_FILE)
    await optimizer_router.handle_media(message, state, bot=bot, settings=local_settings(tmp_path),
                                        session=None)
    assert message.answers == [texts.ERROR_TOO_LARGE]
    assert jobs.created == [] and bot.downloads == []


def test_files_far_above_20mb_are_within_the_local_limit(tmp_path, clean_env):
    settings = local_settings(tmp_path)
    assert not IncomingFile(file_id="X", kind=FileKind.VIDEO, size=1500 * MB).exceeds(
        settings.input_limit_bytes)


# --- menu, prompt, copy -----------------------------------------------------


def test_media_optimizer_sits_after_voice_note_in_the_menu():
    buttons = [b for row in main_menu().inline_keyboard for b in row]
    labels = [b.text for b in buttons]
    assert labels.index("🗜 Media Optimizer") == labels.index("🎙 Voice Note") + 1
    assert buttons[labels.index("🗜 Media Optimizer")].callback_data == \
        MenuCallback(action="optimize").pack()


def test_the_prompt_starts_with_the_specified_copy():
    assert texts.OPTIMIZE_PROMPT.startswith(
        "🗜 Send me a photo or video.\n\n"
        "I'll reduce the file size while keeping it suitable for Telegram.\n\n"
        "Large files are supported."
    )
    assert "File" in texts.OPTIMIZE_PROMPT  # recommends sending as a File


def test_help_describes_the_optimizer():
    assert ("🗜 <b>Media Optimizer</b>\nReduces photo and video file sizes while keeping them "
            "suitable for Telegram.") in texts.HELP


async def test_opening_the_tool_prompts_and_records_the_feature(session):
    context = fsm_for()
    message = FakeMessage()
    await optimizer_router.open_optimizer(FakeCallback(message), context, session=session)
    assert await context.get_state() == OptimizerStates.waiting_for_media.state
    assert message.edits == [texts.OPTIMIZE_PROMPT]
    count = await session.scalar(select(func.count()).select_from(FeatureEvent)
                                 .where(FeatureEvent.feature == "media_optimize"))
    assert count == 1


@pytest.mark.parametrize(
    "size,label",
    [(850 * 1024, "850 KB"), (int(4.2 * MB), "4.2 MB"), (148 * MB, "148 MB"),
     (int(1.4 * 1024 * MB), "1.4 GB")],
)
def test_sizes_read_naturally(size, label):
    assert texts.format_size(size) == label


def test_the_summary_matches_the_specified_shape():
    text = texts.video_summary(width=1920, height=1080, duration=102, size_bytes=148 * MB,
                               codec="h264", frame_rate=29.97, bitrate=11_600_000, has_audio=True)
    assert text.splitlines()[:2] == ["🎬 <b>Video detected</b>", "1920×1080 • 01:42 • 148 MB"]
    assert text.splitlines()[2] == "H.264 • 29.97 fps • 11.6 Mbps • with audio"


def test_the_done_message_matches_the_specified_shape():
    assert texts.optimize_done(148 * MB, 31 * MB) == (
        "✅ Optimized\n\nBefore: 148 MB\nAfter: 31 MB\nSaved: 79%")


def test_preset_buttons_carry_estimates_and_a_cancel():
    buttons = [b for row in preset_choices(["small", "balanced", "high"],
                                           {"small": 18 * MB, "balanced": 35 * MB,
                                            "high": 58 * MB}).inline_keyboard for b in row]
    assert [b.text for b in buttons] == [
        "⚡ Small — ~18 MB", "⚖️ Balanced — ~35 MB", "💎 High Quality — ~58 MB", "❌ Cancel"]
    assert OptimizeCallback.unpack(buttons[1].callback_data).value == "balanced"
    plain = [b.text for row in preset_choices(["small"]).inline_keyboard for b in row]
    assert plain == ["⚡ Small", "❌ Cancel"]


def test_the_done_keyboard_offers_another_and_the_menu():
    buttons = [b for row in optimize_done().inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["🗜 Optimize Another", "🏠 Main Menu"]
    assert buttons[0].callback_data == MenuCallback(action="optimize").pack()


# --- progress ---------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def test_progress_is_shown_at_milestones_without_spamming():
    status = FakeMessage()
    clock = Clock()
    report = optimizer_router.ProgressReporter(status, min_interval=15, clock=clock)

    for fraction, at in [(0.1, 5), (0.3, 10), (0.31, 20), (0.4, 22), (0.55, 30), (0.6, 40),
                         (0.8, 50), (0.9, 60), (1.0, 70)]:
        clock.now = at
        await report(fraction)

    # 25% was reached at 10 s - too soon - so it shows at 20 s; then 50% and 75%.
    assert status.edits == [texts.optimize_progress(25), texts.optimize_progress(50),
                            texts.optimize_progress(75)]


async def test_a_quick_job_shows_no_progress_at_all():
    status = FakeMessage()
    clock = Clock()
    report = optimizer_router.ProgressReporter(status, clock=clock)
    for fraction in (0.25, 0.5, 0.75, 1.0):
        clock.now += 1
        await report(fraction)
    assert status.edits == []


# --- real video, through the service ----------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize(
    "name,expected",
    [
        ("4k", {Preset.SMALL: (1280, 720), Preset.BALANCED: (1920, 1080), Preset.HIGH: (1920, 1080)}),
        ("1080p", {Preset.SMALL: (1280, 720), Preset.BALANCED: (1920, 1080), Preset.HIGH: (1920, 1080)}),
        ("720p", {Preset.SMALL: (1280, 720), Preset.BALANCED: (1280, 720), Preset.HIGH: (1280, 720)}),
        ("portrait", {Preset.SMALL: (720, 1280), Preset.BALANCED: (1080, 1920), Preset.HIGH: (1080, 1920)}),
        ("square", {Preset.SMALL: (720, 720), Preset.BALANCED: (1080, 1080), Preset.HIGH: (1080, 1080)}),
    ],
)
async def test_every_preset_scales_correctly_and_shrinks_the_file(name, expected, videos, tmp_path):
    source = videos(name)
    before = probe(source)
    optimizer = service()
    analysis_ = await optimizer.analyze(source)
    sizes = {}

    for preset, (width, height) in expected.items():
        plan = plan_video(analysis_, preset)
        result = await optimizer.optimize(source, workspace_in(tmp_path), analysis_, preset)
        after = probe(result.path)
        assert (after["width"], after["height"]) == (width, height)
        assert after["video_codec"] == "h264" and after["pix_fmt"] == "yuv420p"
        assert after["audio_codecs"] == ["aac"]
        assert after["duration"] == pytest.approx(before["duration"], abs=0.35)
        assert after["fps"] == pytest.approx(before["fps"], abs=0.1)     # frame rate kept
        # Aspect ratio kept (to the even-pixel rounding).
        assert width / height == pytest.approx(before["width"] / before["height"], rel=0.01)
        assert result.size_bytes == result.path.stat().st_size < source.stat().st_size
        # The estimate shown before processing was honest.
        assert 0.5 <= result.size_bytes / plan.estimated_bytes <= 1.2, (preset, result.size_bytes,
                                                                         plan.estimated_bytes)
        sizes[preset] = result.size_bytes

    assert sizes[Preset.SMALL] < sizes[Preset.BALANCED] < sizes[Preset.HIGH]


@needs_ffmpeg
async def test_a_rotated_phone_video_comes_out_upright(videos, tmp_path):
    source = videos("rotated")
    stored = probe(source)
    assert (stored["width"], stored["height"]) == (1920, 1080) and stored["rotation"] in (90, -90)

    analysis_ = await service().analyze(source)
    assert (analysis_.width, analysis_.height) == (1080, 1920)
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.SMALL)

    after = probe(result.path)
    assert (after["width"], after["height"]) == (720, 1280)   # upright pixels...
    assert after["rotation"] == 0                              # ...and no matrix left to apply


@needs_ffmpeg
async def test_silent_video_stays_silent(videos, tmp_path):
    source = videos("silent")   # MKV, no audio
    analysis_ = await service().analyze(source)
    assert not analysis_.has_audio
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.BALANCED)
    after = probe(result.path)
    assert after["audio_codecs"] == [] and after["videos"] == 1
    assert "mp4" in after["format"]


@needs_ffmpeg
async def test_audio_is_preserved_and_surround_folded_to_stereo(videos, tmp_path):
    source = videos("surround")
    assert probe(source)["audio_channels"] == [6]
    analysis_ = await service().analyze(source)
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.HIGH)
    after = probe(result.path)
    assert after["audio_codecs"] == ["aac"] and after["audio_channels"] == [2]


@needs_ffmpeg
async def test_slow_motion_is_brought_down_to_60fps(videos, tmp_path):
    source = videos("slowmo")
    analysis_ = await service().analyze(source)
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.SMALL)
    after = probe(result.path)
    assert after["fps"] == pytest.approx(60, abs=0.5)
    assert after["duration"] == pytest.approx(2, abs=0.2)


@needs_ffmpeg
async def test_output_is_faststart_and_carries_none_of_the_source_metadata(videos, tmp_path):
    source = videos("tagged")
    assert probe(source)["tags"]["title"] == "Private title"
    analysis_ = await service().analyze(source)
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.SMALL)

    boxes = top_level_boxes(result.path)
    assert boxes.index("moov") < boxes.index("mdat")     # playable before fully downloaded
    tags = probe(result.path)["tags"]
    assert not {"title", "location", "comment", "creation_time"} & set(tags)


@needs_ffmpeg
@pytest.mark.parametrize("name", ["webm", "mov"])
async def test_other_containers_become_h264_mp4(name, videos, tmp_path):
    source = videos(name)
    analysis_ = await service().analyze(source)
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.BALANCED)
    after = probe(result.path)
    assert "mp4" in after["format"] and after["video_codec"] == "h264"
    assert after["audio_codecs"] == ["aac"]


@needs_ffmpeg
async def test_a_long_video_keeps_its_length_and_reports_progress(videos, tmp_path):
    source = videos("long")
    analysis_ = await service().analyze(source)
    fractions: list[float] = []

    async def on_progress(fraction):
        fractions.append(fraction)

    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.SMALL,
                                      on_progress=on_progress)

    assert probe(result.path)["duration"] == pytest.approx(150, abs=0.5)
    assert fractions and fractions == sorted(fractions)
    assert fractions[-1] >= 0.95
    assert any(0.2 < f < 0.8 for f in fractions)     # progress arrived during the encode


# --- real photos, through the service -------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize("name", ["large_jpeg", "portrait_jpeg", "landscape_jpeg"])
async def test_jpeg_keeps_its_exact_dimensions_and_shrinks(name, images, tmp_path):
    source = images(name)
    with Image.open(source) as original:
        dimensions = original.size
    analysis_ = await service().analyze(source)
    assert isinstance(analysis_, ImageAnalysis) and analysis_.format == "jpeg"
    sizes = {}

    for preset in Preset:
        result = await service().optimize(source, workspace_in(tmp_path), analysis_, preset)
        with Image.open(result.path) as optimized:
            assert optimized.format == "JPEG"
            assert optimized.size == dimensions
            assert optimized.info.get("progressive") or optimized.info.get("progression")
        assert probe(result.path)["video_codec"] == "mjpeg"
        sizes[preset] = result.size_bytes
    assert sizes[Preset.SMALL] <= sizes[Preset.BALANCED] <= sizes[Preset.HIGH] < source.stat().st_size


@needs_ffmpeg
async def test_jpeg_keeps_orientation_and_colour_but_drops_location_and_device(images, tmp_path):
    source = images("exif_jpeg")
    with Image.open(source) as seeded:
        assert seeded.getexif().get(0x010F) == "Samsung" and seeded.getexif().get_ifd(0x8825)
    analysis_ = await service().analyze(source)
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.BALANCED)

    with Image.open(source) as original, Image.open(result.path) as optimized:
        assert optimized.size == original.size == (1600, 1200)   # pixels not rotated
        exif = optimized.getexif()
        assert exif.get(0x0112) == 6                               # still displays upright
        assert 0x010F not in exif and 0x0110 not in exif           # no Make / Model
        assert not exif.get_ifd(0x8825)                            # no GPS
        assert optimized.info.get("icc_profile") == original.info.get("icc_profile")


@needs_ffmpeg
@pytest.mark.parametrize("name", ["png", "transparent_png", "flat_png", "palette_png"])
async def test_png_is_optimized_losslessly(name, images, tmp_path):
    source = images(name)
    analysis_ = await service().analyze(source)
    for preset in Preset:
        result = await service().optimize(source, workspace_in(tmp_path), analysis_, preset)
        with Image.open(result.path) as optimized, Image.open(source) as original:
            assert optimized.format == "PNG"                      # never turned into JPEG
            assert optimized.size == original.size
        assert same_pixels(source, result.path)                   # every pixel, alpha included
        assert result.size_bytes < source.stat().st_size


@needs_ffmpeg
async def test_transparency_survives(images, tmp_path):
    source = images("transparent_png")
    analysis_ = await service().analyze(source)
    assert analysis_.has_alpha
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.SMALL)
    with Image.open(result.path) as optimized:
        assert optimized.mode == "RGBA"
        assert optimized.getchannel("A").getextrema() == (0, 255)
    assert probe(result.path)["pix_fmt"] == "rgba"


@needs_ffmpeg
async def test_palette_transparency_survives(images, tmp_path):
    source = images("palette_png")
    analysis_ = await service().analyze(source)
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.HIGH)
    with Image.open(result.path) as optimized:
        assert optimized.info.get("transparency") == 0
        assert optimized.convert("RGBA").getpixel((0, 0))[3] == 0


@needs_ffmpeg
async def test_a_16_bit_png_keeps_every_bit(images, tmp_path):
    source = images("deep_png")
    analysis_ = await service().analyze(source)
    assert analysis_.deep_png
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.SMALL)
    after = probe(result.path)
    assert after["pix_fmt"] == "rgb48be"
    assert decoded_md5(result.path) == decoded_md5(source)
    assert result.size_bytes < source.stat().st_size


@needs_ffmpeg
async def test_lossy_webp_is_recompressed_at_the_same_size(images, tmp_path):
    source = images("webp")
    analysis_ = await service().analyze(source)
    assert analysis_.format == "webp"
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.BALANCED)
    with Image.open(result.path) as optimized:
        assert optimized.format == "WEBP" and optimized.size == (1600, 1200)
    assert result.size_bytes < source.stat().st_size


@needs_ffmpeg
async def test_lossless_webp_stays_lossless(images, tmp_path):
    source = images("lossless_webp")
    analysis_ = await service().analyze(source)
    result = await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.SMALL)
    assert image_worker._webp_is_lossless(result.path)
    assert same_pixels(source, result.path)


@needs_ffmpeg
async def test_an_animated_webp_is_refused(images, tmp_path):
    source = images("animated_webp")
    with pytest.raises(MediaProcessingError) as excinfo:
        analysis_ = await service().analyze(source)
        await service().optimize(source, workspace_in(tmp_path), analysis_, Preset.SMALL)
    assert excinfo.value.code in (ProcessingErrorCode.UNSUPPORTED, ProcessingErrorCode.PROBE_FAILED)


# --- the full flow, through the router ----------------------------------------------


@needs_ffmpeg
async def test_analysis_shows_a_summary_and_estimated_presets(videos, tmp_path, clean_env, jobs,
                                                              file_server):
    source = videos("1080p")
    file_server.source = source
    message = FakeMessage()
    context = fsm_for()
    settings = local_settings(tmp_path)

    analysis_ = await optimizer_router._analyze(
        message=message, state=context, bot=FakeBot(SERVER_FILE, size=source.stat().st_size),
        settings=settings, session=None,
        incoming=incoming_for(source, kind=FileKind.VIDEO), user_id=42,
    )

    text = message.answers[-1]
    lines = text.splitlines()
    assert lines[0] == "🎬 <b>Video detected</b>"
    assert lines[1] == f"1920×1080 • 00:03 • {texts.format_size(source.stat().st_size)}"
    assert lines[2].startswith("H.264 • 30 fps • ") and lines[2].endswith(" • with audio")
    assert text.endswith("Choose optimization:")
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    plans = plan_all(analysis_)
    assert labels == [
        f"⚡ Small — ~{texts.format_size(plans[Preset.SMALL].estimated_bytes)}",
        f"⚖️ Balanced — ~{texts.format_size(plans[Preset.BALANCED].estimated_bytes)}",
        f"💎 High Quality — ~{texts.format_size(plans[Preset.HIGH].estimated_bytes)}",
        "❌ Cancel",
    ]
    assert await context.get_state() == OptimizerStates.choosing_preset.state
    assert (await context.get_data())["optimize_file"]["file_id"] == f"F-{source.stem}"
    # Nothing was encoded, no job recorded, the local copy is gone - and the
    # server keeps its copy for the choice.
    assert jobs.created == []
    assert workspaces_left(settings) == []
    assert file_server.deleted == []


@needs_ffmpeg
async def test_photo_analysis_offers_presets_without_estimates(images, tmp_path, clean_env, jobs):
    source = images("png")
    _, message, context = await analyze_borrowed(tmp_path, source)
    text = message.answers[-1]
    assert text.startswith(f"🖼 <b>Photo detected</b>\n1200×900 • PNG • "
                           f"{texts.format_size(source.stat().st_size)}")
    assert texts.OPTIMIZE_PNG_NOTE in text
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == ["⚡ Small", "⚖️ Balanced", "💎 High Quality", "❌ Cancel"]
    assert await context.get_state() == OptimizerStates.choosing_preset.state


@needs_ffmpeg
async def test_a_telegram_compressed_upload_gets_the_send_as_file_hint(images, tmp_path, clean_env, jobs):
    _, message, _ = await analyze_borrowed(tmp_path, images("landscape_jpeg"), compressed=True)
    assert texts.OPTIMIZE_COMPRESSED_NOTE in message.answers[-1]


@needs_ffmpeg
async def test_an_already_small_video_is_called_well_optimized(videos, tmp_path, clean_env, jobs,
                                                               file_server):
    source = videos("tiny")
    file_server.source = source
    message, context = FakeMessage(), fsm_for()
    settings = local_settings(tmp_path)

    await optimizer_router._analyze(
        message=message, state=context, bot=FakeBot(SERVER_FILE, size=source.stat().st_size),
        settings=settings, session=None, incoming=incoming_for(source, kind=FileKind.VIDEO),
        user_id=42,
    )

    assert message.answers[-1].endswith("\n\n✅ This file is already well optimized.")
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == ["🗜 Optimize Another", "🏠 Main Menu"]      # no presets to pick
    assert await context.get_state() is None
    assert file_server.deleted                                  # nothing left to wait for


@needs_ffmpeg
async def test_an_already_small_video_is_never_made_larger(videos, tmp_path, clean_env, jobs):
    source = videos("tiny")
    for preset in Preset:
        outcome, message, _ = await optimize_borrowed(tmp_path, source, preset)
        assert outcome is optimizer_router._Outcome.ALREADY_EFFICIENT
        assert message.documents == []
        assert message.answers[-1] == "✅ Your original file is already efficiently compressed."


@needs_ffmpeg
async def test_a_video_is_optimized_and_sent_back_as_a_file(videos, tmp_path, clean_env, jobs,
                                                           file_server):
    source = videos("4k")
    file_server.source = source
    context = fsm_for()
    await context.set_state(OptimizerStates.choosing_preset)
    message = FakeMessage(keep=tmp_path / "received")
    settings = local_settings(tmp_path)

    outcome = await optimizer_router._run_optimize_job(
        message=message, state=context, bot=FakeBot(SERVER_FILE, size=source.stat().st_size),
        settings=settings, session=None,
        incoming=incoming_for(source, kind=FileKind.VIDEO, name="Holiday 4K.MOV"),
        preset=Preset.BALANCED, user_id=42,
    )

    assert outcome is optimizer_router._Outcome.OPTIMIZED, message.answers
    (document,) = message.documents
    assert document["filename"] == "atreox_optimized_Holiday_4K.mp4"
    assert document["no_detection"] is True                     # stays a File
    after = document["probe"]
    assert (after["width"], after["height"]) == (1920, 1080)
    assert after["video_codec"] == "h264" and after["audio_codecs"] == ["aac"]
    before_bytes, after_bytes = source.stat().st_size, document["size"]
    assert message.answers[-1] == texts.optimize_done(before_bytes, after_bytes)
    assert message.answers[-1].startswith("✅ Optimized\n\nBefore: ")
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == ["🗜 Optimize Another", "🏠 Main Menu"]
    assert jobs.created[0]["job_type"] is JobType.MEDIA_OPTIMIZE
    assert jobs.status == "success" and jobs.output_size == after_bytes
    assert await context.get_state() is None
    # Cleanup: source copy, encoded file and workspace gone; server copy released.
    assert not document["workspace"].exists()
    assert workspaces_left(settings) == []
    assert file_server.deleted == file_server.requested


@needs_ffmpeg
@pytest.mark.parametrize("name", ["large_jpeg", "transparent_png", "webp"])
async def test_a_photo_is_optimized_and_sent_back_as_a_file(name, images, tmp_path, clean_env, jobs):
    source = images(name)
    outcome, message, settings = await optimize_borrowed(tmp_path, source, Preset.BALANCED)

    assert outcome is optimizer_router._Outcome.OPTIMIZED, message.answers
    (document,) = message.documents
    assert document["filename"] == f"atreox_optimized_{source.stem}{source.suffix.lower()}"
    assert document["no_detection"] is True
    with Image.open(source) as original, Image.open(document["copy"]) as optimized:
        assert optimized.size == original.size and optimized.format == original.format
    assert document["size"] < source.stat().st_size
    assert workspaces_left(settings) == []
    assert source.exists()                                     # the borrowed copy is untouched


@needs_ffmpeg
async def test_an_already_optimized_photo_is_not_replaced(images, tmp_path, clean_env, jobs):
    source = images("optimized_png")
    outcome, message, settings = await optimize_borrowed(tmp_path, source, Preset.HIGH)

    assert outcome is optimizer_router._Outcome.ALREADY_EFFICIENT
    assert message.documents == []
    assert message.answers[-1] == texts.OPTIMIZE_ALREADY_COMPRESSED
    assert jobs.status == "success" and jobs.output_size is None
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_cleanup_and_a_retry_after_an_ffmpeg_failure(videos, tmp_path, clean_env, jobs,
                                                           file_server, monkeypatch):
    source = videos("720p")
    file_server.source = source
    real_args = optimizer_media.build_video_args

    def broken(*args, **kwargs):
        arguments = real_args(*args, **kwargs)
        arguments[arguments.index("libx264")] = "no_such_encoder"
        return arguments

    monkeypatch.setattr(optimizer_media, "build_video_args", broken)
    context = fsm_for()
    await context.set_state(OptimizerStates.choosing_preset)
    await context.set_data({"optimize_file": incoming_for(source, kind=FileKind.VIDEO).to_dict()})
    message = FakeMessage()
    settings = local_settings(tmp_path)

    await optimizer_router.handle_preset(
        FakeCallback(message), OptimizeCallback(action="preset", value="small"), context,
        bot=FakeBot(SERVER_FILE, size=source.stat().st_size), settings=settings, session=None,
    )

    assert message.documents == []
    assert message.answers[-1] == texts.ERROR_PROCESSING
    assert jobs.status == "failed" and jobs.error_code == "encode_failed"
    assert workspaces_left(settings) == []
    # The presets stay usable: another one can be tried on the same file.
    assert await context.get_state() == OptimizerStates.choosing_preset.state
    assert file_server.deleted == []
    assert_nothing_leaked(message, settings)


@needs_ffmpeg
async def test_a_timeout_is_reported_and_cleaned_up(videos, tmp_path, clean_env, jobs, monkeypatch):
    source = videos("long")
    monkeypatch.setattr(optimizer_router, "_optimizer_service", lambda settings: service(timeout=0.3))
    outcome, message, settings = await optimize_borrowed(tmp_path, source, Preset.HIGH)
    assert outcome is optimizer_router._Outcome.FAILED
    assert message.answers[-1] == texts.OPTIMIZE_TIMEOUT
    assert jobs.error_code == "processing_timeout"
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_an_output_over_the_configured_limit_is_not_sent(videos, tmp_path, clean_env, jobs):
    source = videos("4k")
    settings = local_settings(tmp_path, bot_api_local_dir=str(source.parent),
                              max_output_file_size_mb=1)
    outcome, message, _ = await optimize_borrowed(tmp_path, source, Preset.HIGH, settings=settings)
    assert outcome is optimizer_router._Outcome.FAILED
    assert message.documents == []
    assert message.answers[-1] == texts.ERROR_OUTPUT_TOO_LARGE
    assert jobs.error_code == "output_too_large"
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_an_output_that_fails_verification_is_not_sent(videos, tmp_path, clean_env, jobs,
                                                             monkeypatch):
    source = videos("720p")
    real_args = optimizer_media.build_video_args

    def wrong_size(*args, **kwargs):
        arguments = real_args(*args, **kwargs)
        vf = arguments.index("-vf") + 1
        arguments[vf] = arguments[vf].replace("scale=1280:720", "scale=640:360")
        return arguments

    monkeypatch.setattr(optimizer_media, "build_video_args", wrong_size)
    outcome, message, settings = await optimize_borrowed(tmp_path, source, Preset.SMALL)
    assert outcome is optimizer_router._Outcome.FAILED
    assert message.documents == []
    assert message.answers[-1] == texts.OPTIMIZE_VERIFY_FAILED
    assert jobs.error_code == "verify_failed"
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_corrupt_file_is_explained(tmp_path, clean_env, jobs):
    folder = tmp_path / "server"
    folder.mkdir()
    broken = folder / "clip.mp4"
    broken.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"definitely not a video " * 2000)
    _, message, context = await analyze_borrowed(tmp_path, broken)
    assert message.answers[-1] == texts.OPTIMIZE_CORRUPT
    assert jobs.status == "failed" and jobs.error_code == "probe_failed"
    assert await context.get_state() == OptimizerStates.waiting_for_media.state


async def test_running_out_of_disk_is_explained(tmp_path, clean_env, jobs, monkeypatch, file_server):
    class FullDisk:
        async def analyze(self, source, *, job_id=None):
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(optimizer_router, "_optimizer_service", lambda settings: FullDisk())
    file_server.chunks = 1
    message = FakeMessage()
    outcome = await optimizer_router._run_optimize_job(
        message=message, state=fsm_for(), bot=FakeBot(SERVER_FILE, size=MB),
        settings=local_settings(tmp_path), session=None,
        incoming=IncomingFile(file_id="X", kind=FileKind.VIDEO, size=MB), preset=Preset.SMALL,
        user_id=42,
    )
    assert outcome is optimizer_router._Outcome.FAILED
    assert message.answers[-1] == texts.OPTIMIZE_DISK_FULL
    assert jobs.error_code == "disk_full"


async def test_a_disk_short_gate_answers_before_any_work(tmp_path, clean_env, jobs, state):
    gate = MediaJobGate(max_heavy_jobs=2, temp_root=tmp_path, free_bytes=lambda _: 600 * MB)
    message = FakeMessage(document=_doc("big.mp4", "video/mp4", size=500 * MB))
    await optimizer_router.handle_media(message, state, bot=FakeBot(SERVER_FILE),
                                        settings=local_settings(tmp_path), session=None,
                                        media_gate=gate)
    assert message.answers == [texts.OPTIMIZE_DISK_FULL]
    assert jobs.created == []


async def test_a_busy_server_keeps_the_presets_usable(tmp_path, clean_env, jobs):
    gate = MediaJobGate(max_heavy_jobs=1, temp_root=tmp_path, free_bytes=lambda _: 100 * 1024 * MB)
    gate.acquire(99, expected_bytes=MB, heavy=True)
    context = fsm_for()
    await context.set_state(OptimizerStates.choosing_preset)
    await context.set_data({"optimize_file": IncomingFile(
        file_id="X", kind=FileKind.VIDEO, size=300 * MB).to_dict()})
    message = FakeMessage()

    await optimizer_router.handle_preset(
        FakeCallback(message), OptimizeCallback(action="preset", value="high"), context,
        bot=FakeBot(SERVER_FILE), settings=local_settings(tmp_path), session=None, media_gate=gate,
    )

    assert message.answers == [texts.SERVER_BUSY]
    assert jobs.created == []
    assert await context.get_state() == OptimizerStates.choosing_preset.state


async def test_cancel_returns_to_the_menu():
    context = fsm_for()
    await context.set_state(OptimizerStates.choosing_preset)
    message = FakeMessage()
    callback = FakeCallback(message)
    await optimizer_router.cancel_optimizer(callback, context)
    assert await context.get_state() is None
    assert message.edits == [texts.MAIN_MENU]
    assert callback.answers == [texts.CANCELLED]


async def test_a_stale_preset_tap_is_explained_not_executed():
    callback = FakeCallback(FakeMessage())
    await optimizer_router.stale_preset(callback)
    assert callback.answers == [texts.OPTIMIZE_CHOICE_EXPIRED]


# --- sendDocument against the real aiogram client --------------------------------


class RecordingSession(BaseSession):
    """A real aiogram session whose only fake part is the HTTP round trip."""

    def __init__(self, source: Path):
        super().__init__()
        self.source = source
        self.calls: list = []
        self.uploads: list[dict] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetFile):
            result = {"file_id": method.file_id, "file_unique_id": "u",
                      "file_size": self.source.stat().st_size, "file_path": str(self.source)}
        elif isinstance(method, DeleteMessage):
            result = True
        else:
            result = {"message_id": len(self.calls) + 1, "date": 0,
                      "chat": {"id": 42, "type": "private"},
                      "from": {"id": 1, "is_bot": True, "first_name": "Atreox Tools"}}
            if isinstance(method, SendDocument):
                self.uploads.append(self._capture(bot, method, timeout))
                result["document"] = {"file_id": "BQAC", "file_unique_id": "d",
                                      "file_name": method.document.filename}
            else:
                result["text"] = "…"
        return self.check_response(bot, method, 200, json.dumps({"ok": True, "result": result})).result

    def _capture(self, bot, method, timeout) -> dict:
        form = AiohttpSession().build_form_data(bot, method)
        fields = {dict(options)["name"]: (dict(options), value) for options, _h, value in form._fields}
        attachment = fields["document"][1].removeprefix("attach://")
        return {
            "api_method": method.__api_method__,
            "filename": fields[attachment][0]["filename"],
            "no_detection": fields["disable_content_type_detection"][1],
            "streamed": not isinstance(fields[attachment][1], (bytes, bytearray)),
            "timeout": timeout,
            "probe": probe(Path(method.document.path)),
        }

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):
        yield b""

    async def close(self):
        pass


@needs_ffmpeg
async def test_the_result_goes_out_as_a_real_send_document(videos, tmp_path, clean_env, monkeypatch):
    source = videos("720p")
    monkeypatch.setattr(optimizer_router, "Message", Message)
    recorder = FakeJobs()
    monkeypatch.setattr(optimizer_router, "JobsRepository", lambda s: recorder)
    session = RecordingSession(source)
    bot = Bot(token=TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    message = Message.model_validate(
        {"message_id": 1, "date": 0, "chat": {"id": 42, "type": "private"},
         "from": {"id": 42, "is_bot": False, "first_name": "User"}}, context={"bot": bot})

    outcome = await optimizer_router._run_optimize_job(
        message=message, state=fsm_for(), bot=bot,
        settings=local_settings(tmp_path, bot_api_local_dir=str(source.parent)), session=None,
        incoming=incoming_for(source, kind=FileKind.VIDEO, name="clip.mov"),
        preset=Preset.SMALL, user_id=42,
    )

    assert outcome is optimizer_router._Outcome.OPTIMIZED
    (upload,) = session.uploads
    assert upload["api_method"] == "sendDocument"
    assert upload["filename"] == "atreox_optimized_clip.mp4"
    assert upload["no_detection"] == "true"       # Telegram must not turn it into a video
    assert upload["streamed"] is True and upload["timeout"] >= 120
    assert upload["probe"]["video_codec"] == "h264"
    assert not any(getattr(m, "__api_method__", "") in ("sendVideo", "sendPhoto") for m in session.calls)


# --- analytics --------------------------------------------------------------


async def test_optimizations_count_only_files_actually_delivered(session):
    repo = JobsRepository(session)

    async def job(job_type, *, output_size=None, failed=False):
        record = await repo.create(job_id=uuid.uuid4(), telegram_user_id=7, job_type=job_type)
        if failed:
            await repo.mark_failed(record, error_code="encode_failed")
        else:
            await repo.mark_success(record, output_size=output_size)

    await job(JobType.MEDIA_OPTIMIZE, output_size=10 * MB)
    await job(JobType.MEDIA_OPTIMIZE, output_size=2 * MB)
    await job(JobType.MEDIA_OPTIMIZE)                      # already efficient: nothing sent
    await job(JobType.MEDIA_OPTIMIZE, failed=True)
    await job(JobType.CIRCLE, output_size=MB)
    await job(JobType.VOICE_NOTE, output_size=MB)

    stats = await StatsRepository(session).collect()
    assert stats.optimizations == 2
    assert stats.circles == 1 and stats.voice_notes == 1
    report = texts.stats_report(stats)
    assert "🗜 Optimizations: 2" in report
    assert report.index("🎙 Voice notes") < report.index("🗜 Optimizations") < report.index("🧹 Metadata")


async def test_the_chosen_preset_is_recorded(tmp_path, clean_env, session, monkeypatch):
    async def fake_job(**kwargs):
        return optimizer_router._Outcome.OPTIMIZED

    monkeypatch.setattr(optimizer_router, "_run_optimize_job", fake_job)
    for preset in ("small", "high", "high"):
        context = fsm_for()
        await context.set_state(OptimizerStates.choosing_preset)
        await context.set_data({"optimize_file": IncomingFile(file_id="X", kind=FileKind.IMAGE).to_dict()})
        await optimizer_router.handle_preset(
            FakeCallback(FakeMessage()), OptimizeCallback(action="preset", value=preset), context,
            bot=FakeBot(SERVER_FILE), settings=local_settings(tmp_path), session=session,
        )
    rows = (await session.execute(select(FeatureEvent.feature))).scalars().all()
    assert sorted(rows) == ["optimize_high", "optimize_high", "optimize_small"]


def test_feature_and_job_names_fit_their_columns():
    assert JobType.MEDIA_OPTIMIZE.value == "media_optimize"
    assert all(len(f.value) <= 32 for f in Feature)

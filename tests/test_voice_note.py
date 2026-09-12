"""Phase 7: Audio / Video -> native Telegram voice message.

Real ffmpeg/ffprobe produce and verify every voice note here (those tests skip
on a machine without them). Telegram is faked at two depths: a light fake for
the job plumbing, and a real aiogram ``Bot`` whose HTTP transport alone is
replaced, to prove the upload really is a ``sendVoice`` carrying OGG/Opus.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, GetFile, SendVoice
from aiogram.types import Message
from sqlalchemy import func, select

from app.bot import texts
from app.bot.callbacks import MenuCallback, VoiceCallback
from app.bot.keyboards.common import main_menu
from app.bot.keyboards.voice import voice_done, voice_forward_help
from app.bot.routers import voice as voice_router
from app.bot.states import VoiceStates
from app.db.models import Feature, FeatureEvent, JobType
from app.db.repositories import JobsRepository, StatsRepository
from app.services import telegram_files
from app.services.jobgate import MediaJobGate
from app.services.media import voice as voice_media
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.voice import (
    FfmpegVoiceNoteService,
    VoiceOutputInfo,
    build_voice_ffmpeg_args,
    format_is_allowed,
    parse_audio_source,
    verify_voice_output,
)
from app.services.telegram_files import FileKind, IncomingFile, extract_voice_source
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

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


# --- ffprobe, as the tests' own source of truth ------------------------------


def ffprobe(path: Path) -> dict:
    """Container, codec, layout and duration, straight from ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", "--", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    return {
        "format": data["format"]["format_name"],
        "codecs": [s["codec_name"] for s in data["streams"]],
        "codec": audio[0]["codec_name"] if audio else None,
        "channels": audio[0].get("channels") if audio else None,
        "sample_rate": int(audio[0]["sample_rate"]) if audio else None,
        "duration": float(data["format"].get("duration") or 0),
    }


def assert_native_voice(probe: dict, *, duration: float, tolerance: float = 0.25) -> None:
    """What Telegram needs to draw a voice bubble, and nothing else."""
    assert probe["format"] == "ogg"
    assert probe["codecs"] == ["opus"]          # one stream: no video, no cover art
    assert probe["channels"] == 1
    assert probe["sample_rate"] == 48000
    assert probe["duration"] == pytest.approx(duration, abs=tolerance)


# --- real media fixtures ------------------------------------------------------

_TONE = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]
_STEREO = ["-f", "lavfi", "-i", "aevalsrc=sin(440*2*PI*t)|sin(660*2*PI*t):s=44100"]
_PICTURE = ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10"]
_H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]

# name -> (ffmpeg input/codec arguments, extension)
_RECIPES = {
    "mp3": (_TONE + ["-c:a", "libmp3lame", "-b:a", "64k"], ".mp3"),
    "wav_stereo": (_STEREO + ["-c:a", "pcm_s16le"], ".wav"),
    "m4a": (_TONE + ["-c:a", "aac"], ".m4a"),
    "aac": (_TONE + ["-c:a", "aac", "-f", "adts"], ".aac"),
    "ogg_vorbis": (_TONE + ["-c:a", "libvorbis"], ".ogg"),
    "flac_stereo": (_STEREO + ["-c:a", "flac"], ".flac"),
    "opus": (_TONE + ["-c:a", "libopus"], ".opus"),
    "mp4": (_PICTURE + _TONE + _H264 + ["-c:a", "aac"], ".mp4"),
    "mov": (_PICTURE + _TONE + _H264 + ["-c:a", "aac"], ".mov"),
    "webm": (_PICTURE + _TONE + ["-c:v", "libvpx", "-deadline", "realtime", "-c:a", "libopus"], ".webm"),
    "mkv_stereo": (_PICTURE + _STEREO + _H264 + ["-c:a", "aac"], ".mkv"),
    "mp4_silent": (_PICTURE + _H264, ".mp4"),
}


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Real media files, generated once per module and shared read-only."""
    root = tmp_path_factory.mktemp("voice-media")
    cache: dict = {}

    def get(kind: str, seconds: float = 3) -> Path:
        key = (kind, seconds)
        if key not in cache:
            args, extension = _RECIPES[kind]
            # Each source gets its own directory, which doubles as the Bot API
            # server's --dir when a test borrows the file in place.
            folder = root / f"{kind}-{str(seconds).replace('.', '_')}"
            folder.mkdir()
            path = folder / f"source{extension}"
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args,
                 "-t", str(seconds), str(path)],
                check=True, timeout=300,
            )
            cache[key] = path
        return cache[key]

    return get


# --- fakes ------------------------------------------------------------------


class FakeMessage:
    """Records what a handler sent; probes each voice note before it is deleted."""

    def __init__(self, user_id: int = 42, **media):
        self.chat = SimpleNamespace(id=user_id)
        self.from_user = SimpleNamespace(id=user_id, is_bot=False)
        for attribute in ("audio", "voice", "video", "video_note", "document", "photo",
                          "sticker", "text"):
            setattr(self, attribute, None)
        for attribute, value in media.items():
            setattr(self, attribute, value)
        self.answers: list[str] = []
        self.markups: list = []
        self.edits: list[str] = []
        self.voices: list[dict] = []
        self.on_voice = None

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return FakeMessage(self.from_user.id)

    async def answer_voice(self, voice=None, **kwargs):
        # The job deletes the file as soon as this returns.
        path = Path(voice.path)
        if self.on_voice is not None:
            self.on_voice()
        self.voices.append({
            "filename": voice.filename,
            "duration_arg": kwargs.get("duration"),
            "size": path.stat().st_size,
            "workspace": path.parent,
            "probe": ffprobe(path),
        })
        return SimpleNamespace(voice=SimpleNamespace(duration=kwargs.get("duration")))

    async def edit_text(self, text=None, **kwargs):
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))
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


@pytest_asyncio.fixture
async def state():
    context = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=42))
    await context.set_state(VoiceStates.waiting_for_media)
    yield context
    await context.clear()


def fsm_for(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(voice_router, "JobsRepository", lambda session: recorder)
    return recorder


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(voice_router, "Message", FakeMessage)


@pytest.fixture
def file_server(monkeypatch):
    server = FakeFileServer()
    monkeypatch.setattr(telegram_files, "AiohttpFileClient", lambda: server)
    return server


def incoming_for(path: Path, *, kind=FileKind.AUDIO, file_id="SRC-1", name=None, size=None):
    return IncomingFile(
        file_id=file_id, kind=kind, size=path.stat().st_size if size is None else size,
        original_filename=name or path.name, mime_type=None,
    )


async def run_borrowed(tmp_path, source: Path, *, message=None, state=None, kind=FileKind.AUDIO,
                       settings=None, gate=None, user_id=42):
    """A voice job whose source sits on the Bot API server's disk, used in place."""
    settings = settings or local_settings(tmp_path, bot_api_local_dir=str(source.parent))
    message = message or FakeMessage(user_id)
    state = state or fsm_for(user_id)
    outcome = await voice_router._run_voice_job(
        message=message, state=state, bot=FakeBot(str(source), size=source.stat().st_size),
        settings=settings, session=None, incoming=incoming_for(source, kind=kind),
        user_id=user_id, user=None, media_gate=gate,
    )
    return outcome, message, settings


async def run_streamed(tmp_path, source: Path, file_server: FakeFileServer, *, message=None,
                       state=None, gate=None, settings=None):
    """A voice job whose source is streamed from the Bot API server's file endpoint."""
    file_server.source = source
    settings = settings or local_settings(tmp_path)
    message = message or FakeMessage()
    outcome = await voice_router._run_voice_job(
        message=message, state=state or fsm_for(42),
        bot=FakeBot(SERVER_FILE, size=source.stat().st_size), settings=settings, session=None,
        incoming=incoming_for(source, name="recording.wav"), user_id=42, user=None,
        media_gate=gate,
    )
    return outcome, message, settings


def assert_nothing_leaked(message: FakeMessage, settings) -> None:
    """Users only ever see our own copy - never a path, never an exception."""
    for answer in message.answers:
        assert str(settings.temp_root) not in answer
        assert "Traceback" not in answer and "Error" not in answer and "ffmpeg" not in answer


# --- FFmpeg arguments (pure) ------------------------------------------------


def test_voice_args_encode_mono_48k_opus_in_ogg_for_speech():
    args = build_voice_ffmpeg_args("ffmpeg", Path("/in/source.mp3"), Path("/out/voice.ogg"))
    pairs = dict(zip(args, args[1:]))
    assert pairs["-c:a"] == "libopus"
    assert pairs["-f"] == "ogg"
    assert pairs["-ac"] == "1"
    assert pairs["-ar"] == "48000"
    assert pairs["-application"] == "voip"
    assert pairs["-b:a"] == "48k"
    # The first audio stream only; pictures, subtitles and tags are dropped.
    assert pairs["-map"] == "0:a:0"
    assert {"-vn", "-sn", "-dn"} <= set(args)
    assert pairs["-map_metadata"] == "-1"
    assert args[-1] == str(Path("/out/voice.ogg"))


def test_voice_args_never_touch_pitch_tempo_or_loudness():
    args = build_voice_ffmpeg_args("ffmpeg", Path("/in.wav"), Path("/out.ogg"))
    assert not {"-af", "-filter:a", "-filter_complex", "-filter"} & set(args)
    joined = " ".join(args)
    for effect in ("atempo", "asetrate", "rubberband", "loudnorm", "dynaudnorm", "volume"):
        assert effect not in joined


# --- ffprobe parsing (pure) -------------------------------------------------


def probe_json(*streams, format_name="mp3", duration="3.0") -> str:
    return json.dumps({"streams": list(streams),
                       "format": {"format_name": format_name, "duration": duration}})


def test_an_audio_file_is_parsed():
    info = parse_audio_source(probe_json(
        {"codec_type": "audio", "codec_name": "mp3", "channels": 2, "sample_rate": "44100",
         "duration": "12.5"}))
    assert (info.audio_codec, info.channels, info.sample_rate) == ("mp3", 2, 44100)
    assert info.duration == 12.5 and info.has_video is False


def test_duration_falls_back_to_the_container():
    info = parse_audio_source(probe_json({"codec_type": "audio", "codec_name": "opus"},
                                         format_name="matroska,webm", duration="7.25"))
    assert info.duration == 7.25


def test_a_video_without_audio_is_reported_as_such():
    with pytest.raises(MediaProcessingError) as excinfo:
        parse_audio_source(probe_json({"codec_type": "video", "codec_name": "h264"},
                                      format_name="mov,mp4,m4a,3gp,3g2,mj2"))
    assert excinfo.value.code is ProcessingErrorCode.NO_AUDIO


def test_cover_art_alone_is_not_a_video():
    with pytest.raises(MediaProcessingError) as excinfo:
        parse_audio_source(probe_json(
            {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}))
    assert excinfo.value.code is ProcessingErrorCode.UNSUPPORTED


def test_cover_art_is_ignored_next_to_real_audio():
    info = parse_audio_source(probe_json(
        {"codec_type": "audio", "codec_name": "mp3"},
        {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}))
    assert info.has_video is False


@pytest.mark.parametrize("format_name", ["hls", "concat", "png_pipe", "image2", "tty", "srt", ""])
def test_demuxers_outside_the_allowlist_are_refused(format_name):
    assert format_is_allowed(format_name) is False
    with pytest.raises(MediaProcessingError) as excinfo:
        parse_audio_source(probe_json({"codec_type": "audio", "codec_name": "mp3"},
                                      format_name=format_name))
    assert excinfo.value.code is ProcessingErrorCode.UNSUPPORTED


@pytest.mark.parametrize(
    "format_name",
    ["mp3", "wav", "flac", "ogg", "aac", "mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm", "avi", "asf"],
)
def test_common_formats_are_allowed(format_name):
    assert format_is_allowed(format_name) is True


def test_garbage_probe_output_is_a_probe_failure():
    with pytest.raises(MediaProcessingError) as excinfo:
        parse_audio_source("not json")
    assert excinfo.value.code is ProcessingErrorCode.PROBE_FAILED


@pytest.mark.parametrize(
    "info,code",
    [
        (VoiceOutputInfo("matroska,webm", "opus", 1, 48000, 3.0), ProcessingErrorCode.VERIFY_FAILED),
        (VoiceOutputInfo("ogg", "vorbis", 1, 48000, 3.0), ProcessingErrorCode.VERIFY_FAILED),
        (VoiceOutputInfo("ogg", "opus", 1, 48000, 0.0), ProcessingErrorCode.EMPTY_OUTPUT),
    ],
)
def test_output_that_would_not_be_a_voice_bubble_is_refused(info, code):
    with pytest.raises(MediaProcessingError) as excinfo:
        verify_voice_output(info)
    assert excinfo.value.code is code


def test_a_real_voice_output_passes_verification():
    verify_voice_output(VoiceOutputInfo("ogg", "opus", 1, 48000, 3.0))


# --- what counts as input -------------------------------------------------------


def _media(**fields):
    return SimpleNamespace(file_id="F", file_size=1000, **fields)


@pytest.mark.parametrize(
    "attribute,fields,kind",
    [
        ("audio", {"file_name": "song.mp3", "mime_type": "audio/mpeg", "duration": 180}, FileKind.AUDIO),
        ("voice", {"mime_type": "audio/ogg", "duration": 4}, FileKind.AUDIO),
        ("video", {"file_name": "clip.mp4", "mime_type": "video/mp4", "duration": 30}, FileKind.VIDEO),
        ("video_note", {"duration": 12}, FileKind.VIDEO),
    ],
)
def test_native_audio_and_video_are_accepted(attribute, fields, kind):
    incoming = extract_voice_source(FakeMessage(**{attribute: _media(**fields)}))
    assert incoming is not None and incoming.kind is kind
    assert incoming.duration == fields["duration"]


@pytest.mark.parametrize(
    "file_name,mime_type,kind",
    [
        ("talk.mp3", "audio/mpeg", FileKind.AUDIO),
        ("talk.wav", "audio/x-wav", FileKind.AUDIO),
        ("talk.m4a", "audio/mp4", FileKind.AUDIO),
        ("talk.aac", "audio/aac", FileKind.AUDIO),
        ("talk.ogg", "application/ogg", FileKind.AUDIO),
        ("talk.flac", "audio/flac", FileKind.AUDIO),
        ("talk.opus", "application/octet-stream", FileKind.AUDIO),
        ("clip.mp4", "video/mp4", FileKind.VIDEO),
        ("clip.mov", "video/quicktime", FileKind.VIDEO),
        ("clip.webm", "video/webm", FileKind.VIDEO),
        ("clip.mkv", "video/x-matroska", FileKind.VIDEO),
        ("clip.mkv", "application/octet-stream", FileKind.VIDEO),
    ],
)
def test_audio_and_video_documents_are_accepted(file_name, mime_type, kind):
    document = _media(file_name=file_name, mime_type=mime_type)
    incoming = extract_voice_source(FakeMessage(document=document))
    assert incoming is not None and incoming.kind is kind
    assert incoming.original_filename == file_name


@pytest.mark.parametrize(
    "message",
    [
        FakeMessage(text="hello"),
        FakeMessage(photo=[_media()]),
        FakeMessage(sticker=_media()),
        FakeMessage(document=_media(file_name="report.pdf", mime_type="application/pdf")),
        FakeMessage(document=_media(file_name="photo.jpg", mime_type="image/jpeg")),
        FakeMessage(document=_media(file_name="setup.exe", mime_type="application/octet-stream")),
    ],
    ids=["text", "photo", "sticker", "pdf", "image-document", "unknown-binary"],
)
def test_everything_else_is_refused(message):
    assert extract_voice_source(message) is None


def test_audio_sources_keep_a_safe_audio_extension_on_disk():
    assert IncomingFile(file_id="X", kind=FileKind.AUDIO, original_filename="Talk.FLAC").extension == ".flac"
    assert IncomingFile(file_id="X", kind=FileKind.AUDIO, original_filename="x.mp3;rm -rf").extension == ".bin"
    assert IncomingFile(file_id="X", kind=FileKind.AUDIO, original_filename="list.m3u8").extension == ".bin"
    # Photo and video jobs never pick up an audio extension.
    assert IncomingFile(file_id="X", kind=FileKind.VIDEO, original_filename="a.m4a").extension == ".mp4"


# --- menu, prompt, help -----------------------------------------------------


def test_voice_note_sits_second_in_the_main_menu():
    buttons = [b for row in main_menu().inline_keyboard for b in row]
    assert [b.text for b in buttons] == [
        "🎥 Video → Circle", "🎙 Voice Note", "🧹 Metadata Studio",
        "🗜 Media Optimizer", "🖼 Watermark", "📦 Batch Mode",
        "🎞 GIF / MP4", "🖼 Extract Frame", "🏷 Make Sticker",
        "🎭 Find Stickers", "🚀 Grow My Channel", "ℹ️ Help",
    ]
    assert buttons[1].callback_data == MenuCallback(action="voice").pack()


def test_the_prompt_copy_is_exactly_as_specified():
    assert texts.VOICE_PROMPT == (
        "🎙 Send me an audio file or a video.\n\n"
        "I'll turn its audio into a native Telegram voice message.\n\n"
        "You can send large files too."
    )


def test_help_describes_the_voice_note():
    assert "🎙 <b>Voice Note</b>" in texts.HELP
    assert "Turns audio, or the audio track from a video, into a native Telegram " in texts.HELP
    # Help lists the tools in the same order as the menu.
    assert texts.HELP.index("Video → Circle") < texts.HELP.index("Voice Note")
    assert texts.HELP.index("Voice Note") < texts.HELP.index("Metadata Studio")


async def test_opening_the_tool_prompts_and_records_the_feature(session):
    context = fsm_for(42)
    message = FakeMessage()
    callback = FakeCallback(message)

    await voice_router.open_voice(callback, context, session=session)

    assert await context.get_state() == VoiceStates.waiting_for_media.state
    assert message.edits == [texts.VOICE_PROMPT]
    events = await session.scalar(
        select(func.count()).select_from(FeatureEvent)
        .where(FeatureEvent.feature == Feature.VOICE_NOTE.value)
    )
    assert events == 1


def test_success_keyboard_offers_another_forwarding_help_and_menu():
    buttons = [b for row in voice_done().inline_keyboard for b in row]
    assert [b.text for b in buttons] == [
        "🎙 Make Another", '🤖 Hide "Forwarded from bot"', "🏠 Main Menu",
    ]
    assert buttons[0].callback_data == MenuCallback(action="voice").pack()
    assert buttons[1].callback_data == VoiceCallback(action="forward_help").pack()
    assert buttons[2].callback_data == MenuCallback(action="main").pack()


async def test_forwarding_help_speaks_about_the_voice_message_and_returns_to_it():
    message = FakeMessage()
    await voice_router.show_forward_help(FakeCallback(message))
    assert "Press and hold the voice message → Forward" in message.edits[-1]
    assert "Hide Sender Name" in message.edits[-1]

    await voice_router.show_ios_guide(FakeCallback(message))
    await voice_router.show_android_guide(FakeCallback(message))
    assert message.edits[-2].startswith("🍎") and message.edits[-1].startswith("🤖")

    await voice_router.back_to_result(FakeCallback(message))
    assert message.edits[-1] == texts.VOICE_DONE
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == [b.text for row in voice_done().inline_keyboard for b in row]
    # Back leads to the voice result, never the circle one.
    back = [b for row in voice_forward_help().inline_keyboard for b in row][-1]
    assert back.callback_data == VoiceCallback(action="forward_back").pack()


def test_the_circle_forwarding_guide_is_unchanged():
    assert texts.CIRCLE_FORWARD_HELP.startswith(
        "🤖 <b>How to hide the bot name when forwarding</b>\n\n"
        "1. Press and hold the circle → Forward\n"
    )


# --- handler guards ---------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        FakeMessage(text="hi"),
        FakeMessage(sticker=_media()),
        FakeMessage(document=_media(file_name="notes.pdf", mime_type="application/pdf")),
    ],
    ids=["text", "sticker", "pdf"],
)
async def test_unsupported_media_is_explained_and_the_tool_stays_open(message, tmp_path, clean_env, state):
    bot = FakeBot(SERVER_FILE)
    await voice_router.handle_voice_source(message, state, bot=bot,
                                           settings=local_settings(tmp_path), session=None)
    assert message.answers == [texts.VOICE_UNSUPPORTED]
    assert bot.downloads == []
    assert await state.get_state() == VoiceStates.waiting_for_media.state


async def test_an_oversized_file_is_refused_before_fetching(tmp_path, clean_env, state, jobs):
    message = FakeMessage(document=SimpleNamespace(
        file_id="BIG", file_size=2001 * MB, file_name="podcast.wav", mime_type="audio/wav"))
    bot = FakeBot(SERVER_FILE)
    await voice_router.handle_voice_source(message, state, bot=bot,
                                           settings=local_settings(tmp_path), session=None)
    assert message.answers == [texts.ERROR_TOO_LARGE]
    assert jobs.created == []
    assert await state.get_state() == VoiceStates.waiting_for_media.state


def test_large_files_are_not_held_to_the_old_20mb_cap(tmp_path, clean_env):
    settings = local_settings(tmp_path)
    podcast = IncomingFile(file_id="P", kind=FileKind.AUDIO, size=1500 * MB)
    assert settings.uses_local_bot_api
    assert not podcast.exceeds(settings.input_limit_bytes)


async def test_a_busy_server_answers_immediately_and_keeps_the_tool_open(tmp_path, clean_env, state, jobs):
    gate = MediaJobGate(max_heavy_jobs=1, temp_root=tmp_path, free_bytes=lambda _: 100 * 1024 * MB)
    gate.acquire(99, expected_bytes=MB, heavy=True)  # someone else's big job
    message = FakeMessage(document=SimpleNamespace(
        file_id="D", file_size=300 * MB, file_name="long.mp4", mime_type="video/mp4"))

    await voice_router.handle_voice_source(message, state, bot=FakeBot(SERVER_FILE),
                                           settings=local_settings(tmp_path), session=None,
                                           media_gate=gate)

    assert message.answers == [texts.SERVER_BUSY]
    assert jobs.created == []
    assert await state.get_state() == VoiceStates.waiting_for_media.state


# --- real ffmpeg: every format becomes a native voice note ---------------------


@needs_ffmpeg
@pytest.mark.parametrize(
    "kind,file_kind",
    [
        ("mp3", FileKind.AUDIO),
        ("wav_stereo", FileKind.AUDIO),
        ("m4a", FileKind.AUDIO),
        ("aac", FileKind.AUDIO),
        ("ogg_vorbis", FileKind.AUDIO),
        ("flac_stereo", FileKind.AUDIO),
        ("opus", FileKind.AUDIO),
        ("mp4", FileKind.VIDEO),
        ("mov", FileKind.VIDEO),
        ("webm", FileKind.VIDEO),
        ("mkv_stereo", FileKind.VIDEO),
    ],
)
async def test_each_format_becomes_a_native_voice_note(kind, file_kind, media, tmp_path, clean_env, jobs):
    source = media(kind)
    source_duration = ffprobe(source)["duration"]
    context = fsm_for(42)
    await context.set_state(VoiceStates.waiting_for_media)

    outcome, message, settings = await run_borrowed(tmp_path, source, kind=file_kind, state=context)

    assert outcome is voice_router._Outcome.DONE, message.answers
    assert len(message.voices) == 1
    voice = message.voices[0]
    assert_native_voice(voice["probe"], duration=source_duration)
    # Named .ogg so the Bot API server labels it audio/ogg, and a duration sent along.
    assert voice["filename"] == "voice.ogg"
    assert voice["duration_arg"] == max(1, round(voice["probe"]["duration"]))
    # Success UX.
    assert message.answers[-1] == texts.VOICE_DONE
    assert message.markups[-1] is not None
    assert await context.get_state() is None
    assert jobs.created[0]["job_type"] is JobType.VOICE_NOTE
    assert jobs.status == "success" and jobs.output_size == voice["size"]
    assert workspaces_left(settings) == []
    # The server's copy was only borrowed: never modified, never deleted.
    assert source.exists()


@needs_ffmpeg
async def test_stereo_input_is_folded_to_mono_at_48k(media, tmp_path, clean_env, jobs):
    source = media("wav_stereo", seconds=5)
    before = ffprobe(source)
    assert (before["channels"], before["sample_rate"]) == (2, 44100)

    _, message, _ = await run_borrowed(tmp_path, source)

    probe = message.voices[0]["probe"]
    assert (probe["channels"], probe["sample_rate"]) == (1, 48000)
    assert probe["duration"] == pytest.approx(5, abs=0.25)


@needs_ffmpeg
async def test_a_video_without_audio_is_rejected_cleanly(media, tmp_path, clean_env, jobs):
    source = media("mp4_silent")
    context = fsm_for(42)
    await context.set_state(VoiceStates.waiting_for_media)

    outcome, message, settings = await run_borrowed(tmp_path, source, kind=FileKind.VIDEO,
                                                    state=context)

    assert outcome is voice_router._Outcome.FAILED
    assert message.voices == []                              # no empty voice note
    assert message.answers[-1] == "🔇 This video doesn't contain an audio track."
    assert jobs.status == "failed" and jobs.error_code == "no_audio"
    # Still in the tool, so the next file can follow straight away.
    assert await context.get_state() == VoiceStates.waiting_for_media.state
    assert workspaces_left(settings) == []
    assert_nothing_leaked(message, settings)


@needs_ffmpeg
async def test_long_audio_keeps_its_full_length(media, tmp_path, clean_env, jobs):
    source = media("mp3", seconds=900)  # 15 minutes

    outcome, message, _ = await run_borrowed(tmp_path, source)

    assert outcome is voice_router._Outcome.DONE
    probe = message.voices[0]["probe"]
    assert_native_voice(probe, duration=ffprobe(source)["duration"], tolerance=0.5)
    assert message.voices[0]["duration_arg"] == 900
    # Speech bitrate keeps a quarter hour small: ~48 kbit/s.
    assert message.voices[0]["size"] < 8 * MB


@needs_ffmpeg
async def test_a_large_file_streams_through_without_being_held_in_memory(
    media, tmp_path, clean_env, jobs, file_server
):
    source = media("wav_stereo", seconds=130)  # ~22 MB: above the old 20 MB cap
    assert source.stat().st_size > 20 * MB
    gate = MediaJobGate(max_heavy_jobs=2, temp_root=tmp_path / "work")
    message = FakeMessage()
    heavy_during_send = []
    message.on_voice = lambda: heavy_during_send.append(gate.heavy_running)

    tracemalloc.start()
    try:
        outcome, message, settings = await run_streamed(tmp_path, source, file_server,
                                                        message=message, gate=gate)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert outcome is voice_router._Outcome.DONE, message.answers
    assert_native_voice(message.voices[0]["probe"], duration=130, tolerance=0.3)
    # Streamed a chunk at a time: nowhere near the size of the file.
    assert peak < 8 * MB, f"peak {peak / MB:.1f} MB"
    # Counted as a heavy job while it ran, and every slot and byte handed back.
    assert heavy_during_send == [1]
    assert gate.heavy_running == 0 and gate.reserved_bytes == 0
    # The streamed copy is gone locally, and the server was told to drop its own.
    assert workspaces_left(settings) == []
    assert file_server.deleted == file_server.requested


@needs_ffmpeg
async def test_cleanup_after_success(media, tmp_path, clean_env, jobs, file_server):
    source = media("m4a")

    outcome, message, settings = await run_streamed(tmp_path, source, file_server)

    assert outcome is voice_router._Outcome.DONE
    workspace = message.voices[0]["workspace"]
    assert workspace.parent == Path(settings.temp_root)
    assert not workspace.exists()                  # source, encoded OGG, everything
    assert workspaces_left(settings) == []
    assert file_server.deleted                      # the Bot API server's copy too


@needs_ffmpeg
async def test_cleanup_after_an_ffmpeg_failure(media, tmp_path, clean_env, jobs, file_server, monkeypatch):
    source = media("mp3")
    real_args = voice_media.build_voice_ffmpeg_args

    def broken(*args, **kwargs):
        arguments = real_args(*args, **kwargs)
        arguments[arguments.index("libopus")] = "no_such_encoder"
        return arguments

    monkeypatch.setattr(voice_media, "build_voice_ffmpeg_args", broken)
    context = fsm_for(42)

    outcome, message, settings = await run_streamed(tmp_path, source, file_server, state=context)

    assert outcome is voice_router._Outcome.FAILED
    assert message.voices == []
    assert message.answers[-1] == texts.ERROR_PROCESSING
    assert jobs.status == "failed" and jobs.error_code == "encode_failed"
    assert workspaces_left(settings) == []
    assert file_server.deleted
    assert await context.get_state() == VoiceStates.waiting_for_media.state
    assert_nothing_leaked(message, settings)


@needs_ffmpeg
async def test_a_timeout_is_reported_and_cleaned_up(media, tmp_path, clean_env, jobs, monkeypatch):
    source = media("mp3", seconds=900)
    monkeypatch.setattr(voice_router, "_voice_service", lambda settings: FfmpegVoiceNoteService(
        ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe", timeout=0.05, probe_timeout=60))

    outcome, message, settings = await run_borrowed(tmp_path, source)

    assert outcome is voice_router._Outcome.FAILED
    assert message.answers[-1] == texts.VOICE_TIMEOUT
    assert jobs.error_code == "processing_timeout"
    assert message.voices == []
    assert workspaces_left(settings) == []


@needs_ffmpeg
@pytest.mark.parametrize(
    "repeat,code",
    [
        (100, "probe_failed"),     # not even a readable header
        (4000, "corrupt_input"),   # passes ffprobe, then fails to decode
    ],
)
async def test_corrupt_audio_is_explained(repeat, code, tmp_path, clean_env, jobs):
    folder = tmp_path / "server"
    folder.mkdir()
    broken = folder / "song.mp3"
    broken.write_bytes(b"this is not really an mp3 " * repeat)

    outcome, message, settings = await run_borrowed(tmp_path, broken)

    assert outcome is voice_router._Outcome.FAILED
    assert message.voices == []
    assert message.answers[-1] == texts.VOICE_CORRUPT
    assert jobs.error_code == code
    assert workspaces_left(settings) == []
    assert_nothing_leaked(message, settings)


@pytest.mark.parametrize(
    "stderr,code",
    [
        ("[mp3float] Header missing\nDecode error rate 1 exceeds maximum", "corrupt_input"),
        ("x.flac: Invalid data found when processing input", "corrupt_input"),
        ("Unknown encoder 'libopus'", "encode_failed"),
        ("", "encode_failed"),
    ],
)
def test_ffmpeg_failures_are_told_apart_from_damaged_input(stderr, code):
    assert voice_media.classify_encode_failure(stderr).value == code


@needs_ffmpeg
async def test_a_file_that_is_not_media_at_all_is_unsupported(tmp_path, clean_env, jobs):
    folder = tmp_path / "server"
    folder.mkdir()
    picture = folder / "cover.png"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=32x32", "-frames:v", "1", str(picture)], check=True)

    outcome, message, _ = await run_borrowed(tmp_path, picture)

    assert outcome is voice_router._Outcome.FAILED
    assert message.answers[-1] == texts.VOICE_UNSUPPORTED
    assert jobs.error_code == "unsupported_file"


@needs_ffmpeg
async def test_a_playlist_is_never_followed(media, tmp_path, clean_env, jobs):
    """An HLS playlist would make FFmpeg open other files; it is refused."""
    folder = tmp_path / "server"
    folder.mkdir()
    shutil.copyfile(media("mp3"), folder / "segment.mp3")
    playlist = folder / "list.m3u8"
    playlist.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXTINF:3.0,\nsegment.mp3\n#EXT-X-ENDLIST\n"
    )

    outcome, message, _ = await run_borrowed(tmp_path, playlist)

    assert outcome is voice_router._Outcome.FAILED
    assert message.voices == []
    assert jobs.error_code == "unsupported_file"


@needs_ffmpeg
async def test_an_output_too_large_to_send_is_not_sent(media, tmp_path, clean_env, jobs, monkeypatch):
    source = media("mp3", seconds=900)
    settings = local_settings(tmp_path, bot_api_local_dir=str(source.parent),
                              max_output_file_size_mb=1)

    outcome, message, _ = await run_borrowed(tmp_path, source, settings=settings)

    assert outcome is voice_router._Outcome.FAILED
    assert message.voices == []
    assert message.answers[-1] == texts.ERROR_OUTPUT_TOO_LARGE
    assert jobs.error_code == "output_too_large"
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_concurrent_voice_jobs_do_not_mix_files(media, tmp_path, clean_env, monkeypatch):
    short_mono = media("mp3", seconds=3)
    long_stereo = media("flac_stereo", seconds=7)
    settings = local_settings(tmp_path, bot_api_local_dir=str(short_mono.parent.parent))
    gate = MediaJobGate(max_heavy_jobs=2, temp_root=settings.temp_root)
    recorders: dict = {}
    monkeypatch.setattr(voice_router, "JobsRepository",
                        lambda session: recorders.setdefault(len(recorders), FakeJobs()))
    alice, bob = FakeMessage(1), FakeMessage(2)

    results = await asyncio.gather(
        run_borrowed(tmp_path, short_mono, message=alice, settings=settings, gate=gate, user_id=1),
        run_borrowed(tmp_path, long_stereo, message=bob, settings=settings, gate=gate, user_id=2),
    )

    assert [outcome for outcome, _, _ in results] == [voice_router._Outcome.DONE] * 2
    # Each user got exactly their own recording back.
    assert alice.voices[0]["probe"]["duration"] == pytest.approx(3, abs=0.25)
    assert bob.voices[0]["probe"]["duration"] == pytest.approx(7, abs=0.25)
    # From separate workspaces, both gone now.
    assert alice.voices[0]["workspace"] != bob.voices[0]["workspace"]
    assert workspaces_left(settings) == []
    assert all(r.status == "success" for r in recorders.values())
    assert gate.heavy_running == 0 and gate.reserved_bytes == 0


# --- sendVoice against the real aiogram client ----------------------------------


class RecordingSession(BaseSession):
    """A real aiogram session whose only fake part is the HTTP round trip.

    Requests are serialised exactly as they would go on the wire (multipart for
    uploads), and answers are parsed by aiogram's own response checker.
    """

    def __init__(self, *, source: Path, voice_reply: dict | None = None, error: tuple | None = None):
        super().__init__()
        self.source = source
        self.voice_reply = voice_reply
        self.error = error
        self.calls: list = []
        self.uploads: list[dict] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        status, payload = 200, {"ok": True, "result": self._result(bot, method)}
        if isinstance(method, SendVoice):
            self.uploads.append(self._capture(bot, method, timeout))
            if self.error is not None:
                status, description = self.error
                payload = {"ok": False, "error_code": status, "description": description}
        return self.check_response(bot, method, status, json.dumps(payload)).result

    def _result(self, bot, method):
        if isinstance(method, GetFile):
            return {"file_id": method.file_id, "file_unique_id": "u1",
                    "file_size": self.source.stat().st_size, "file_path": str(self.source)}
        if isinstance(method, DeleteMessage):
            return True
        message = {"message_id": len(self.calls) + 1, "date": 0,
                   "chat": {"id": 42, "type": "private"},
                   "from": {"id": 1, "is_bot": True, "first_name": "Atreox Tools"}}
        if isinstance(method, SendVoice):
            message.update(self.voice_reply or {})
        else:
            message["text"] = "…"
        return message

    def _capture(self, bot, method, timeout) -> dict:
        form = AiohttpSession().build_form_data(bot, method)
        fields = {dict(options)["name"]: (dict(options), value)
                  for options, _headers, value in form._fields}
        attachment = fields["voice"][1].removeprefix("attach://")
        file_part = fields[attachment][0]
        # Aiogram streams the upload; read what it would send, from disk.
        path = Path(method.voice.path)
        return {
            "api_method": method.__api_method__,
            "chat_id": fields["chat_id"][1],
            "duration": fields["duration"][1],
            "filename": file_part["filename"],
            "timeout": timeout,
            "probe": ffprobe(path),
            "streamed": not isinstance(fields[attachment][1], (bytes, bytearray)),
        }

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):
        yield b""

    async def close(self):
        pass


_VOICE_REPLY = {"voice": {"file_id": "AwACAgIAAxkBAAIB", "file_unique_id": "AgADvoice",
                          "duration": 3, "mime_type": "audio/ogg", "file_size": 20000}}
_DOCUMENT_REPLY = {"document": {"file_id": "BQACAgIAAxkBAAIB", "file_unique_id": "AgADdoc",
                                "file_name": "voice.ogg", "mime_type": "audio/ogg"}}


def real_bot(session: RecordingSession) -> Bot:
    return Bot(token=TOKEN, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def real_message(bot: Bot) -> Message:
    return Message.model_validate(
        {"message_id": 1, "date": 0, "chat": {"id": 42, "type": "private"},
         "from": {"id": 42, "is_bot": False, "first_name": "User"}},
        context={"bot": bot},
    )


async def run_through_aiogram(tmp_path, source: Path, session: RecordingSession, monkeypatch):
    # A real aiogram Message is what the router checks for; drop the fake.
    monkeypatch.setattr(voice_router, "Message", Message)
    recorder = FakeJobs()
    monkeypatch.setattr(voice_router, "JobsRepository", lambda s: recorder)
    bot = real_bot(session)
    settings = local_settings(tmp_path, bot_api_local_dir=str(source.parent))
    context = fsm_for(42)
    await context.set_state(VoiceStates.waiting_for_media)
    outcome = await voice_router._run_voice_job(
        message=real_message(bot), state=context, bot=bot, settings=settings, session=None,
        incoming=incoming_for(source), user_id=42,
    )
    return outcome, recorder, settings


def sent_texts(session: RecordingSession) -> list[str]:
    return [m.text for m in session.calls if getattr(m, "__api_method__", "") == "sendMessage"]


@needs_ffmpeg
async def test_the_upload_is_a_real_send_voice_with_ogg_opus(media, tmp_path, clean_env, monkeypatch):
    source = media("mp3")
    session = RecordingSession(source=source, voice_reply=_VOICE_REPLY)

    outcome, recorder, settings = await run_through_aiogram(tmp_path, source, session, monkeypatch)

    assert outcome is voice_router._Outcome.DONE
    (upload,) = session.uploads
    # Telegram's sendVoice - not sendAudio, not sendDocument.
    assert upload["api_method"] == "sendVoice"
    assert not any(getattr(m, "__api_method__", "") in {"sendAudio", "sendDocument"}
                   for m in session.calls)
    assert upload["chat_id"] == "42"
    assert upload["filename"] == "voice.ogg"
    assert upload["duration"] == "3"
    assert upload["streamed"] is True             # read from disk while sending
    assert upload["timeout"] >= 120               # sized to the file, not aiogram's default
    assert_native_voice(upload["probe"], duration=3)
    assert sent_texts(session) == [texts.VOICE_PROCESSING, texts.VOICE_DONE]
    assert recorder.status == "success"
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_reply_that_is_not_a_voice_message_is_taken_back(media, tmp_path, clean_env, monkeypatch):
    source = media("mp3")
    session = RecordingSession(source=source, voice_reply=_DOCUMENT_REPLY)

    outcome, recorder, _ = await run_through_aiogram(tmp_path, source, session, monkeypatch)

    assert outcome is voice_router._Outcome.FAILED
    assert recorder.error_code == "send_failed"
    # The stray document was deleted, and the user was not told "ready".
    sent_voice_id = next(i for i, m in enumerate(session.calls) if isinstance(m, SendVoice)) + 2
    assert any(isinstance(m, DeleteMessage) and m.message_id == sent_voice_id
               for m in session.calls)
    assert sent_texts(session)[-1] == texts.VOICE_SEND_FAILED
    assert texts.VOICE_DONE not in sent_texts(session)


@needs_ffmpeg
async def test_voice_messages_blocked_by_privacy_settings_are_explained(
    media, tmp_path, clean_env, monkeypatch
):
    source = media("mp3")
    session = RecordingSession(source=source,
                               error=(400, "Bad Request: VOICE_MESSAGES_FORBIDDEN"))

    outcome, recorder, settings = await run_through_aiogram(tmp_path, source, session, monkeypatch)

    assert outcome is voice_router._Outcome.FAILED
    assert recorder.error_code == "voice_forbidden"
    assert sent_texts(session)[-1] == texts.VOICE_FORBIDDEN
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_any_other_send_voice_failure_is_friendly(media, tmp_path, clean_env, monkeypatch):
    source = media("mp3")
    session = RecordingSession(source=source, error=(400, "Bad Request: wrong file identifier"))

    outcome, recorder, settings = await run_through_aiogram(tmp_path, source, session, monkeypatch)

    assert outcome is voice_router._Outcome.FAILED
    assert recorder.error_code == "send_failed"
    assert sent_texts(session)[-1] == texts.VOICE_SEND_FAILED
    assert all("wrong file identifier" not in t for t in sent_texts(session))
    assert workspaces_left(settings) == []


async def test_a_rejected_upload_size_maps_to_the_output_limit_copy(tmp_path, monkeypatch):
    class TooLarge(FakeBot):
        async def __call__(self, method, request_timeout=None):
            method.close()
            from aiogram.exceptions import TelegramEntityTooLarge

            raise TelegramEntityTooLarge(SendVoice(chat_id=1, voice="x"), "Request Entity Too Large")

    result = voice_media.VoiceNoteResult(path=tmp_path / "v.ogg", size_bytes=10,
                                         filename="v.ogg", duration=3.0)
    with pytest.raises(MediaProcessingError) as excinfo:
        await voice_router._send_voice(TooLarge(SERVER_FILE), FakeMessage(), result)
    assert excinfo.value.code is ProcessingErrorCode.OUTPUT_TOO_LARGE
    assert voice_router._failure_text(excinfo.value) == texts.ERROR_OUTPUT_TOO_LARGE


def test_telegram_errors_are_matched_on_the_documented_reason():
    error = TelegramBadRequest(SendVoice(chat_id=1, voice="x"),
                               "Bad Request: VOICE_MESSAGES_FORBIDDEN")
    assert voice_router._VOICE_FORBIDDEN_MARKER in str(error)


# --- analytics --------------------------------------------------------------


async def test_voice_notes_are_counted_in_stats_without_touching_history(session):
    import uuid

    repo = JobsRepository(session)
    for job_type, succeed in (
        (JobType.CIRCLE, True), (JobType.CIRCLE, True), (JobType.METADATA_CLEAN, True),
        (JobType.VOICE_NOTE, True), (JobType.VOICE_NOTE, True), (JobType.VOICE_NOTE, False),
    ):
        job = await repo.create(job_id=uuid.uuid4(), telegram_user_id=7, job_type=job_type)
        if succeed:
            await repo.mark_success(job)
        else:
            await repo.mark_failed(job, error_code="no_audio")

    stats = await StatsRepository(session).collect()

    assert stats.voice_notes == 2            # successful ones only
    assert stats.circles == 2 and stats.metadata_cleans == 1
    assert stats.jobs.total == 6
    report = texts.stats_report(stats)
    assert "🎙 Voice notes: 2" in report
    assert "🎥 Circles: 2" in report
    assert report.index("🎥 Circles") < report.index("🎙 Voice notes") < report.index("🧹 Metadata")


async def test_stats_render_voice_notes_as_zero_on_an_empty_database(session):
    report = texts.stats_report(await StatsRepository(session).collect())
    assert "🎙 Voice notes: 0" in report


def test_the_job_type_and_feature_fit_their_columns():
    assert JobType.VOICE_NOTE.value == "voice_note"
    assert Feature.VOICE_NOTE.value == "voice_note"
    assert len("voice_note") <= 32

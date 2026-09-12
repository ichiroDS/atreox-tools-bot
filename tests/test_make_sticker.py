"""Make Sticker: an image in, a real Telegram sticker back.

The output is checked as Telegram would check it - WEBP, 512 px on the long
side, under the size ceiling - and the sending path asserts that the reply
really carried a sticker, because a sticker that arrives as a file is not what
the user asked for.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bot import texts
from app.bot.callbacks import MakeStickerCallback
from app.bot.routers import make_sticker as sticker_router
from app.bot.states import MakeStickerStates
from app.db.models import JobType
from app.services import telegram_files
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.sticker import (
    MAX_STICKER_BYTES,
    STICKER_FILENAME,
    STICKER_SIDE,
    StickerService,
    StickerStyle,
    verify_sticker,
)
from app.utils.temp_files import JobWorkspace
from tests.test_large_files import (
    SERVER_FILE,
    FakeBot,
    FakeFileServer,
    FakeJobs,
    local_settings,
    workspaces_left,
)
from tests.test_media_optimizer import FakeCallback, FakeMessage, fsm_for, probe

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffprobe") is None, reason="ffprobe not installed"
)


def service(timeout: float = 300) -> StickerService:
    return StickerService(ffprobe_bin="ffprobe", timeout=timeout, probe_timeout=60)


def workspace_in(tmp_path: Path) -> JobWorkspace:
    path = tmp_path / f"ws-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return JobWorkspace(job_id=uuid.uuid4(), path=path)


@pytest.fixture(scope="module")
def images(tmp_path_factory):
    root = tmp_path_factory.mktemp("stickers")
    cache: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name in cache:
            return cache[name]
        folder = root / name
        folder.mkdir()
        if name == "transparent_png":
            path = folder / "mascot.png"
            picture = Image.new("RGBA", (600, 400), (0, 0, 0, 0))
            # A round subject with transparent margins around it - so the
            # trim has something to remove and the corners stay see-through.
            ImageDraw.Draw(picture).ellipse((150, 100, 449, 299), fill=(220, 40, 40, 255))
            picture.save(path)
        elif name == "opaque_jpeg":
            path = folder / "photo.jpg"
            Image.new("RGB", (900, 600), (30, 90, 160)).save(path, quality=95)
        elif name == "webp":
            path = folder / "art.webp"
            Image.new("RGB", (700, 700), (200, 200, 30)).save(path, quality=90)
        elif name == "tiny":
            path = folder / "dot.png"
            Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(path)
        elif name == "wide":
            path = folder / "banner.png"
            picture = Image.new("RGBA", (1200, 300), (0, 0, 0, 0))
            picture.paste((10, 200, 10, 255), (0, 0, 1200, 300))
            picture.save(path)
        elif name == "broken":
            path = folder / "broken.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 40)
        else:  # pragma: no cover - a typo in a test
            raise AssertionError(name)
        cache[name] = path
        return path

    return get


# --- the sticker itself --------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize("name", ["transparent_png", "opaque_jpeg", "webp"])
async def test_every_supported_image_becomes_a_valid_sticker(name, images, tmp_path):
    source = images(name)
    analysis = await service().analyze(source)
    result = await service().make(source, workspace_in(tmp_path), analysis, StickerStyle.CLEAN)

    written = probe(result.path)
    assert written["video_codec"] == "webp"
    assert max(written["width"], written["height"]) == STICKER_SIDE
    assert min(written["width"], written["height"]) <= STICKER_SIDE
    assert result.size_bytes <= MAX_STICKER_BYTES
    assert result.filename == STICKER_FILENAME


@needs_ffmpeg
async def test_the_aspect_ratio_survives_the_512_box(images, tmp_path):
    source = images("wide")                    # 1200x300, 4:1
    analysis = await service().analyze(source)
    result = await service().make(source, workspace_in(tmp_path), analysis, StickerStyle.CLEAN)

    assert result.width == STICKER_SIDE
    assert result.height == pytest.approx(STICKER_SIDE / 4, abs=2)


@needs_ffmpeg
async def test_transparency_is_preserved_and_empty_margins_are_trimmed(images, tmp_path):
    source = images("transparent_png")         # a 300x200 blob inside 600x400
    analysis = await service().analyze(source)
    result = await service().make(source, workspace_in(tmp_path), analysis, StickerStyle.CLEAN)

    assert result.has_alpha is True
    with Image.open(result.path) as opened:
        sticker = opened.convert("RGBA")
        # The subject is 3:2, and it now fills the sticker: the transparent
        # margins are gone rather than scaled along with it.
        assert sticker.width == STICKER_SIDE
        assert sticker.height == pytest.approx(STICKER_SIDE * 2 / 3, abs=3)
        # ...and what was see-through still is.
        assert sticker.getpixel((1, 1))[3] == 0


@needs_ffmpeg
@pytest.mark.parametrize("style,edge", [
    (StickerStyle.WHITE_OUTLINE, (255, 255, 255)),
    (StickerStyle.BLACK_OUTLINE, (0, 0, 0)),
])
async def test_an_outline_is_drawn_around_the_subject(style, edge, images, tmp_path):
    source = images("transparent_png")
    analysis = await service().analyze(source)
    clean = await service().make(source, workspace_in(tmp_path), analysis, StickerStyle.CLEAN)
    outlined = await service().make(source, workspace_in(tmp_path), analysis, style)

    assert max(outlined.width, outlined.height) == STICKER_SIDE
    # The outline sits outside the picture, so the subject is drawn smaller.
    assert (outlined.width, outlined.height) != (clean.width, clean.height)
    with Image.open(outlined.path) as sticker:
        pixels = sticker.convert("RGBA")
        # A pixel just inside the edge of the subject's silhouette is the
        # outline colour, at full opacity.
        sample = pixels.getpixel((pixels.width // 2, 4))
        assert sample[3] == 255
        assert max(abs(sample[index] - edge[index]) for index in range(3)) < 40


@needs_ffmpeg
async def test_an_opaque_photo_keeps_its_own_edges(images, tmp_path):
    """No background is invented: an opaque picture stays a full rectangle."""
    source = images("opaque_jpeg")
    analysis = await service().analyze(source)
    result = await service().make(source, workspace_in(tmp_path), analysis, StickerStyle.CLEAN)

    assert result.has_alpha is False
    with Image.open(result.path) as sticker:
        corners = sticker.convert("RGBA")
        for point in ((0, 0), (corners.width - 1, corners.height - 1)):
            assert corners.getpixel(point)[3] == 255


@needs_ffmpeg
async def test_an_image_too_small_to_be_a_sticker_is_refused(images, tmp_path):
    source = images("tiny")
    analysis = await service().analyze(source)
    with pytest.raises(MediaProcessingError) as error:
        await service().make(source, workspace_in(tmp_path), analysis, StickerStyle.CLEAN)
    assert error.value.code is ProcessingErrorCode.TOO_SMALL


@needs_ffmpeg
async def test_a_corrupt_image_is_refused_before_a_sticker_is_claimed(images, tmp_path):
    with pytest.raises(MediaProcessingError):
        await service().analyze(images("broken"))


def test_verification_refuses_anything_that_is_not_a_real_sticker():
    good = {"streams": [{"codec_type": "video", "codec_name": "webp",
                         "width": 512, "height": 300}]}
    verify_sticker(good, 1000)                      # does not raise

    for bad, size in (
        ({"streams": [{"codec_type": "video", "codec_name": "png",
                       "width": 512, "height": 300}]}, 1000),
        ({"streams": [{"codec_type": "video", "codec_name": "webp",
                       "width": 400, "height": 300}]}, 1000),
        (good, MAX_STICKER_BYTES + 1),
    ):
        with pytest.raises(MediaProcessingError) as error:
            verify_sticker(bad, size)
        assert error.value.code is ProcessingErrorCode.VERIFY_FAILED


# --- the flow, through the router ------------------------------------------------


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(sticker_router, "Message", FakeMessage)


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(sticker_router, "JobsRepository", lambda session: recorder)
    return recorder


@pytest.fixture
def file_server(monkeypatch):
    server = FakeFileServer()
    monkeypatch.setattr(telegram_files, "AiohttpFileClient", lambda: server)
    return server


class StickerMessage(FakeMessage):
    """Records stickers, and can be told to deliver one as something else."""

    def __init__(self, *args, delivers_sticker: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.stickers: list[dict] = []
        self.deleted: list = []
        self._delivers_sticker = delivers_sticker

    async def answer_sticker(self, sticker=None, **kwargs):
        path = Path(sticker.path)
        self.stickers.append({
            "filename": sticker.filename, "size": path.stat().st_size, "probe": probe(path),
        })
        reply = SimpleNamespace(
            sticker=SimpleNamespace(file_id="S-1") if self._delivers_sticker else None,
            document=SimpleNamespace(file_id="D-1"),
            delete=self._record_delete,
        )
        return reply

    async def _record_delete(self):
        self.deleted.append(True)
        return True

    async def edit_text(self, text=None, **kwargs):
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return self


def as_photo(path: Path, **kwargs) -> StickerMessage:
    message = StickerMessage(**kwargs)
    message.document = SimpleNamespace(
        file_id="IMG-1", file_size=path.stat().st_size, file_name=path.name,
        mime_type="image/png" if path.suffix == ".png" else "image/jpeg",
    )
    return message


@needs_ffmpeg
async def test_the_whole_flow_from_menu_to_a_native_sticker(images, tmp_path, clean_env,
                                                            jobs, file_server, session):
    source = images("transparent_png")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_photo(source)
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await sticker_router.open_make_sticker(FakeCallback(message), context, session=session)
    assert message.edits[-1] == texts.STICKER_MAKE_PROMPT

    await sticker_router.handle_image(message, context, settings=settings)
    assert await context.get_state() == MakeStickerStates.choosing_style.state
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == [
        texts.BTN_STICKER_CLEAN, texts.BTN_STICKER_WHITE_OUTLINE,
        texts.BTN_STICKER_BLACK_OUTLINE, texts.BTN_CANCEL,
    ]

    await sticker_router.handle_style(
        FakeCallback(message), MakeStickerCallback(action="style", value="white_outline"),
        context, bot=bot, settings=settings, session=session,
    )

    (sticker,) = message.stickers
    assert sticker["filename"] == STICKER_FILENAME
    assert sticker["probe"]["video_codec"] == "webp"
    assert max(sticker["probe"]["width"], sticker["probe"]["height"]) == STICKER_SIDE
    assert message.documents == [], "a sticker is not sent as a file"
    assert message.answers[-1] == texts.STICKER_MAKE_DONE
    assert jobs.created[0]["job_type"] is JobType.MAKE_STICKER
    assert jobs.status == "success"
    assert await context.get_state() is None
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_sticker_telegram_stored_as_something_else_is_taken_back(images, tmp_path,
                                                                        clean_env, jobs,
                                                                        file_server, session):
    source = images("transparent_png")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_photo(source, delivers_sticker=False)
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await sticker_router.handle_image(message, context, settings=settings)
    await sticker_router.handle_style(
        FakeCallback(message), MakeStickerCallback(action="style", value="clean"),
        context, bot=bot, settings=settings, session=session,
    )

    assert message.deleted, "the wrong message must be taken back"
    assert jobs.status == "failed"
    assert message.answers[-1] == texts.STICKER_MAKE_SEND_FAILED
    # The styles are still on screen, so another one can be tried.
    assert await context.get_state() == MakeStickerStates.choosing_style.state
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_video_is_not_accepted_as_a_sticker(tmp_path, clean_env, settings_free=None):
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = StickerMessage()
    message.video = SimpleNamespace(file_id="V", file_size=1000, file_name="clip.mp4",
                                    mime_type="video/mp4", duration=3)

    await sticker_router.handle_image(message, context, settings=settings)

    assert message.answers[-1] == texts.STICKER_MAKE_UNSUPPORTED
    assert message.stickers == []


@needs_ffmpeg
async def test_a_too_small_image_is_reported_in_plain_words(images, tmp_path, clean_env,
                                                            jobs, file_server, session):
    source = images("tiny")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_photo(source)
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await sticker_router.handle_image(message, context, settings=settings)
    await sticker_router.handle_style(
        FakeCallback(message), MakeStickerCallback(action="style", value="clean"),
        context, bot=bot, settings=settings, session=session,
    )

    assert message.answers[-1] == texts.STICKER_MAKE_TOO_SMALL
    assert jobs.status == "failed"
    assert message.stickers == []
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_no_file_path_ever_reaches_the_user(images, tmp_path, clean_env, jobs,
                                                  file_server, session, monkeypatch):
    source = images("transparent_png")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_photo(source)
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    async def broken(*args, **kwargs):
        raise MediaProcessingError(
            ProcessingErrorCode.ENCODE_FAILED, f"/var/lib/secret/{uuid.uuid4().hex}.webp"
        )

    monkeypatch.setattr(StickerService, "make", broken)
    await sticker_router.handle_image(message, context, settings=settings)
    await sticker_router.handle_style(
        FakeCallback(message), MakeStickerCallback(action="style", value="clean"),
        context, bot=bot, settings=settings, session=session,
    )

    everything_said = " ".join(text for text in message.answers if text)
    assert "/var/lib" not in everything_said
    assert str(tmp_path) not in everything_said
    assert jobs.status == "failed"

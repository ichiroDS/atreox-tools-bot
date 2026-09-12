"""The image / logo watermark, and the presets that store one.

Where the logo landed is measured from the picture itself - a lossless PNG says
exactly which pixels changed - and the stored preset is read back out of a real
database, because "it survives a restart" is the whole point of keeping it
there rather than on the container's disk.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.bot import texts
from app.bot.callbacks import BatchCallback, WatermarkCallback
from app.bot.routers import batch as batch_router
from app.bot.routers import watermark as watermark_router
from app.bot.states import WatermarkStates
from app.db.models import MAX_LOGO_BYTES, MAX_PRESETS_PER_USER, JobType, WatermarkPreset
from app.db.repositories import PresetLimitReached, WatermarkPresetsRepository
from app.services import telegram_files
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.batch import BatchTool, WatermarkProcessor
from app.services.media.watermark import (
    LOGO_SIZE_RATIOS,
    OPACITIES,
    LogoSpec,
    Position,
    Size,
    WatermarkService,
    WatermarkSpec,
    build_logo_filtergraph,
    build_logo_video_args,
    padding_for,
    plan_logo_layout,
    plan_logo_video,
    validate_logo,
)
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
from PIL import Image, ImageChops, ImageDraw  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def service(timeout: float = 300) -> WatermarkService:
    return WatermarkService(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe",
                            timeout=timeout, probe_timeout=60)


def workspace_in(tmp_path: Path) -> JobWorkspace:
    path = tmp_path / f"ws-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return JobWorkspace(job_id=uuid.uuid4(), path=path)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, timeout=300)


def changed_box(before: Path, after: Path):
    with Image.open(before) as first, Image.open(after) as second:
        return ImageChops.difference(first.convert("RGB"), second.convert("RGB")).getbbox()


def mean_level(image, box) -> float:
    """Average brightness inside a box - how much it changed, in one number."""
    crop = image.crop(box)
    return sum(crop.getdata()) / max(1, crop.width * crop.height)


def region_of(box, size) -> str:
    width, height = size
    left, top, right, bottom = box
    horizontal = "left" if right <= width * 0.55 else ("right" if left >= width * 0.45 else "center")
    vertical = "top" if bottom <= height * 0.55 else "bottom"
    return f"{vertical}_{horizontal}"


@pytest.fixture(scope="module")
def assets(tmp_path_factory):
    root = tmp_path_factory.mktemp("logos")
    cache: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name in cache:
            return cache[name]
        folder = root / name
        folder.mkdir()
        if name == "logo_png":
            path = folder / "brand.png"
            picture = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
            ImageDraw.Draw(picture).ellipse((0, 0, 399, 199), fill=(230, 30, 30, 255))
            picture.save(path)
        elif name == "logo_jpeg":
            path = folder / "brand.jpg"
            Image.new("RGB", (300, 300), (250, 250, 10)).save(path, quality=95)
        elif name == "logo_huge_bytes":
            # A real image, but far bigger than a brand mark should ever be.
            path = folder / "huge.png"
            noise = Image.effect_noise((2000, 2000), 120).convert("RGB")
            noise.save(path)
        elif name == "photo":
            path = folder / "shot.png"
            Image.new("RGB", (1600, 900), (20, 20, 20)).save(path)
        elif name == "portrait_photo":
            path = folder / "tall.png"
            Image.new("RGB", (900, 1600), (20, 20, 20)).save(path)
        elif name == "video":
            path = folder / "clip.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                   "-f", "lavfi", "-i", "sine=frequency=440",
                   "-t", "3", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))
        elif name == "silent_video":
            path = folder / "silent.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-t", "2",
                   "-pix_fmt", "yuv420p", str(path))
        elif name == "not_an_image":
            path = folder / "notes.txt"
            path.write_bytes(b"just some text, not a logo at all\n" * 20)
        else:  # pragma: no cover - a typo in a test
            raise AssertionError(name)
        cache[name] = path
        return path

    return get


# --- what counts as a logo ---------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize("name,image_format", [("logo_png", "png"), ("logo_jpeg", "jpeg")])
async def test_a_small_image_is_accepted_as_a_logo(name, image_format, assets):
    analysis = validate_logo(await service().analyze(assets(name)))
    assert analysis.format == image_format


@needs_ffmpeg
async def test_a_video_is_never_a_logo(assets):
    analysis = await service().analyze(assets("video"))
    with pytest.raises(MediaProcessingError) as error:
        validate_logo(analysis)
    assert error.value.code is ProcessingErrorCode.UNSUPPORTED


@needs_ffmpeg
async def test_something_that_is_not_an_image_is_refused(assets):
    with pytest.raises(MediaProcessingError):
        await service().analyze(assets("not_an_image"))


@needs_ffmpeg
async def test_an_oversized_logo_is_refused_by_bytes(assets):
    analysis = await service().analyze(assets("logo_huge_bytes"))
    assert analysis.size_bytes > MAX_LOGO_BYTES
    with pytest.raises(MediaProcessingError) as error:
        validate_logo(analysis)
    assert error.value.code is ProcessingErrorCode.TOO_LARGE


# --- where the logo goes, and how big ------------------------------------------------


@pytest.mark.parametrize("size", list(Size))
def test_the_logo_is_sized_as_a_share_of_the_frame(size):
    layout = plan_logo_layout(1600, 900, 400, 200, LogoSpec(size=size))
    assert layout.width == pytest.approx(1600 * LOGO_SIZE_RATIOS[size], abs=1)
    # ...and its own proportions are kept.
    assert layout.width / layout.height == pytest.approx(2.0, abs=0.05)


def test_the_same_share_holds_on_a_portrait_frame():
    landscape = plan_logo_layout(1600, 900, 400, 200, LogoSpec(size=Size.M))
    portrait = plan_logo_layout(900, 1600, 400, 200, LogoSpec(size=Size.M))
    assert landscape.width / 1600 == pytest.approx(portrait.width / 900, abs=0.005)


def test_a_very_tall_logo_is_capped_by_the_frame_height():
    layout = plan_logo_layout(1600, 900, 100, 2000, LogoSpec(size=Size.L))
    assert layout.height <= 900 * 0.30 + 1
    assert layout.width / layout.height == pytest.approx(0.05, abs=0.01)


@pytest.mark.parametrize("position", list(Position))
def test_every_position_sits_inside_the_frame_with_its_padding(position):
    layout = plan_logo_layout(1600, 900, 400, 200, LogoSpec(position=position))
    padding = padding_for(1600, 900)
    assert layout.x >= 0 and layout.y >= 0
    assert layout.x + layout.width <= 1600
    assert layout.y + layout.height <= 900
    if position in (Position.TOP_LEFT, Position.BOTTOM_LEFT):
        assert layout.x == padding
    if position in (Position.TOP_LEFT, Position.TOP_RIGHT):
        assert layout.y == padding


def test_the_opacity_reaches_the_filter_as_an_alpha_multiplier():
    analysis_stub = SimpleNamespace(
        width=1280, height=720, audio_codec="aac", has_audio=True, video_bitrate=2_000_000,
        audio_stream=1, video_stream=0, duration=3.0,
    )
    plan = plan_logo_video(analysis_stub, LogoSpec(opacity=50), 400, 200)
    graph = build_logo_filtergraph(plan, LogoSpec(opacity=50))
    assert "colorchannelmixer=aa=0.50" in graph
    assert "format=rgba" in graph, "the logo's own transparency must survive"
    assert "overlay=" in graph


# --- photos --------------------------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize("position", list(Position))
async def test_a_logo_lands_where_it_says_on_a_photo(position, assets, tmp_path):
    source = assets("photo")                 # lossless: changed pixels are exact
    logo = assets("logo_png")
    analysis = await service().analyze(source)
    result = await service().apply_logo(
        source, workspace_in(tmp_path), analysis, LogoSpec(position=position), logo
    )

    box = changed_box(source, result.path)
    assert box is not None, "nothing was drawn"
    assert region_of(box, (analysis.width, analysis.height)) == position.value
    padding = padding_for(analysis.width, analysis.height)
    assert box[0] >= padding - 2 and box[1] >= padding - 2
    assert box[2] <= analysis.width - padding + 2
    assert box[3] <= analysis.height - padding + 2


@needs_ffmpeg
async def test_a_photo_keeps_its_format_and_exact_dimensions(assets, tmp_path):
    source = assets("portrait_photo")
    analysis = await service().analyze(source)
    result = await service().apply_logo(
        source, workspace_in(tmp_path), analysis, LogoSpec(), assets("logo_png")
    )

    with Image.open(source) as before, Image.open(result.path) as after:
        assert after.size == before.size == (900, 1600)
        assert after.format == before.format
    assert (result.width, result.height) == (900, 1600)


@needs_ffmpeg
async def test_a_transparent_logo_does_not_paint_a_box(assets, tmp_path):
    """The corners of the logo's bounding box are round: they must be left alone."""
    source = assets("photo")
    analysis = await service().analyze(source)
    result = await service().apply_logo(
        source, workspace_in(tmp_path), analysis,
        LogoSpec(position=Position.TOP_LEFT, size=Size.L, opacity=100), assets("logo_png"),
    )

    box = changed_box(source, result.path)
    with Image.open(result.path) as after:
        # The top-left corner of the logo's box stays the original background.
        assert after.convert("RGB").getpixel((box[0] + 1, box[1] + 1)) == (20, 20, 20)
        # ...while its middle is the logo.
        middle = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        assert after.convert("RGB").getpixel(middle) != (20, 20, 20)


@needs_ffmpeg
@pytest.mark.parametrize("opacity", list(OPACITIES))
async def test_a_lower_opacity_changes_the_picture_less(opacity, assets, tmp_path):
    source = assets("photo")
    analysis = await service().analyze(source)
    result = await service().apply_logo(
        source, workspace_in(tmp_path), analysis, LogoSpec(opacity=opacity), assets("logo_png")
    )
    with Image.open(source) as before, Image.open(result.path) as after:
        difference = ImageChops.difference(before.convert("RGB"), after.convert("RGB"))
        assert sum(difference.convert("L").getdata()) > 0
    # Recorded per opacity so the relationship can be compared across runs.
    assert result.size_bytes > 0


# --- videos ----------------------------------------------------------------------------


@needs_ffmpeg
async def test_a_video_keeps_its_length_size_and_sound(assets, tmp_path):
    source = assets("video")
    analysis = await service().analyze(source)
    result = await service().apply_logo(
        source, workspace_in(tmp_path), analysis, LogoSpec(), assets("logo_png")
    )

    written = probe(result.path)
    assert written["video_codec"] == "h264"
    assert written["audio_codecs"] == ["aac"], "the soundtrack must survive"
    assert (written["width"], written["height"]) == (1280, 720)
    assert written["duration"] == pytest.approx(analysis.duration, abs=0.3)
    assert (result.width, result.height) == (1280, 720)


@needs_ffmpeg
async def test_a_silent_video_stays_silent(assets, tmp_path):
    source = assets("silent_video")
    analysis = await service().analyze(source)
    result = await service().apply_logo(
        source, workspace_in(tmp_path), analysis, LogoSpec(), assets("logo_png")
    )
    assert probe(result.path)["audio_codecs"] == []


@needs_ffmpeg
async def test_the_logo_is_on_the_last_frame_as_well_as_the_first(assets, tmp_path):
    source = assets("silent_video")
    analysis = await service().analyze(source)
    result = await service().apply_logo(
        source, workspace_in(tmp_path), analysis,
        LogoSpec(position=Position.BOTTOM_RIGHT, size=Size.L, opacity=100), assets("logo_png"),
    )

    # The whole frame is re-encoded, so every pixel differs a little. What
    # says "the logo is here" is that one corner differs far more than the
    # rest of the picture - on the last frame as much as on the first.
    for seconds, name in ((0.1, "first"), (1.8, "last")):
        plain = tmp_path / f"plain_{name}.png"
        marked = tmp_path / f"marked_{name}.png"
        ffmpeg("-ss", str(seconds), "-i", str(source), "-frames:v", "1", str(plain))
        ffmpeg("-ss", str(seconds), "-i", str(result.path), "-frames:v", "1", str(marked))

        with Image.open(plain) as before, Image.open(marked) as after:
            difference = ImageChops.difference(
                before.convert("RGB"), after.convert("RGB")
            ).convert("L")
        corner = mean_level(difference, (420, 240, 640, 360))    # bottom right
        elsewhere = mean_level(difference, (0, 0, 220, 120))     # top left
        assert corner > elsewhere * 5, f"no logo on the {name} frame"


@needs_ffmpeg
async def test_the_video_encode_states_its_thread_budget(assets):
    from app.services.media.base import MAX_ENCODE_THREADS

    analysis = await service().analyze(assets("video"))
    plan = plan_logo_video(analysis, LogoSpec(), 400, 200)
    args = build_logo_video_args("ffmpeg", Path("/in.mp4"), Path("/logo.png"),
                                 Path("/out.mp4"), analysis, plan, LogoSpec())

    assert args.index("-threads") < args.index("-i")
    assert 1 <= int(args[args.index("-threads") + 1]) <= MAX_ENCODE_THREADS
    assert "-map_metadata" in args and args[args.index("-map_metadata") + 1] == "-1"


# --- presets in the database -------------------------------------------------------------


async def test_a_logo_preset_keeps_its_image(session, assets):
    payload = assets("logo_png").read_bytes()
    repository = WatermarkPresetsRepository(session)

    preset = await repository.create(
        42, name="Logo 1", kind="logo", text="", logo=payload, logo_format="png",
        position="bottom_right", style="white_shadow", size="m", opacity=75,
    )
    await session.commit()

    (stored,) = (await session.execute(
        select(WatermarkPreset).where(WatermarkPreset.telegram_user_id == 42)
    )).scalars().all()
    assert stored.is_logo is True
    assert stored.logo == payload, "the image itself has to be there next time"
    assert stored.logo_format == "png"
    assert preset.id == stored.id


async def test_text_presets_are_still_text_presets(session):
    repository = WatermarkPresetsRepository(session)
    preset = await repository.create(
        42, name="@luna", text="@luna",
        position="bottom_right", style="white_shadow", size="m", opacity=75,
    )
    assert preset.is_logo is False
    assert preset.kind == "text"
    assert preset.logo is None


async def test_logo_presets_count_towards_the_same_limit(session, assets):
    payload = assets("logo_png").read_bytes()
    repository = WatermarkPresetsRepository(session)
    for index in range(MAX_PRESETS_PER_USER):
        await repository.create(
            7, name=f"Logo {index}", kind="logo", text="", logo=payload, logo_format="png",
            position="bottom_right", style="white_shadow", size="m", opacity=75,
        )
    with pytest.raises(PresetLimitReached):
        await repository.create(
            7, name="one too many", kind="logo", text="", logo=payload, logo_format="png",
            position="bottom_right", style="white_shadow", size="m", opacity=75,
        )


async def test_one_users_logo_is_invisible_to_another(session, assets):
    payload = assets("logo_png").read_bytes()
    repository = WatermarkPresetsRepository(session)
    mine = await repository.create(
        1, name="Logo 1", kind="logo", text="", logo=payload, logo_format="png",
        position="bottom_right", style="white_shadow", size="m", opacity=75,
    )

    assert await repository.get(mine.id, 2) is None       # another user's id
    assert await repository.list_for(2) == []
    assert (await repository.get(mine.id, 1)).logo == payload


# --- the flow, through the router -----------------------------------------------------


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(watermark_router, "Message", FakeMessage)
    monkeypatch.setattr(batch_router, "Message", FakeMessage)


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


class WizardMessage(FakeMessage):
    async def edit_text(self, text=None, **kwargs):
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return self


def as_document(path: Path, mime: str, *, size: int | None = None) -> WizardMessage:
    message = WizardMessage()
    message.document = SimpleNamespace(
        file_id="DOC-1", file_size=size or path.stat().st_size,
        file_name=path.name, mime_type=mime,
    )
    return message


async def walk_to_logo_confirmation(message, context, settings, bot, logo: Path, *,
                                    position=Position.BOTTOM_RIGHT, size=Size.M, opacity="75"):
    """File -> "Image / Logo" -> the logo -> position -> size -> opacity."""
    await watermark_router.handle_media(message, context, settings=settings)
    await watermark_router.choose_type(
        FakeCallback(message), WatermarkCallback(action="type", value="logo"), context
    )
    logo_message = as_document(logo, "image/png")
    await watermark_router.handle_logo(logo_message, context, bot=bot, settings=settings)
    for handler, action, value in (
        (watermark_router.handle_position, "pos", position.value),
        (watermark_router.handle_size, "size", size.value),
        (watermark_router.handle_opacity, "opacity", opacity),
    ):
        await handler(FakeCallback(message), WatermarkCallback(action=action, value=value), context)
    return logo_message


@needs_ffmpeg
async def test_the_whole_logo_flow_from_menu_to_a_watermarked_photo(assets, tmp_path, clean_env,
                                                                    jobs, file_server, session):
    source = assets("photo")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "image/png")
    message.keep = tmp_path / "received"
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await watermark_router.handle_media(message, context, settings=settings)
    assert await context.get_state() == WatermarkStates.choosing_type.state
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == [
        texts.BTN_WATERMARK_TYPE_TEXT, texts.BTN_WATERMARK_TYPE_LOGO,
        texts.BTN_WATERMARK_PRESETS, texts.BTN_CANCEL,
    ]

    await watermark_router.choose_type(
        FakeCallback(message), WatermarkCallback(action="type", value="logo"), context
    )
    assert await context.get_state() == WatermarkStates.waiting_for_logo.state

    # The logo comes from its own message; the file server serves it too.
    file_server.source = assets("logo_png")
    logo_message = as_document(assets("logo_png"), "image/png")
    await watermark_router.handle_logo(logo_message, context, bot=bot, settings=settings)
    assert await context.get_state() == WatermarkStates.choosing_position.state
    assert (await context.get_data())["logo"], "the logo travels with the wizard"

    for handler, action, value in (
        (watermark_router.handle_position, "pos", "bottom_right"),
        (watermark_router.handle_size, "size", "m"),
        (watermark_router.handle_opacity, "opacity", "75"),
    ):
        await handler(FakeCallback(message), WatermarkCallback(action=action, value=value), context)
    assert await context.get_state() == WatermarkStates.confirming.state
    assert message.edits[-1] == texts.watermark_logo_summary(
        position="bottom_right", size="m", opacity=75)

    file_server.source = source
    await watermark_router.apply_watermark(
        FakeCallback(message), context, bot=bot, settings=settings, session=None,
    )

    (document,) = message.documents
    assert document["filename"] == "atreox_watermarked_shot.png"
    assert document["no_detection"] is True
    box = changed_box(source, document["copy"])
    assert region_of(box, (1600, 900)) == "bottom_right"
    assert message.answers[-1] == texts.WATERMARK_DONE
    assert jobs.created[0]["job_type"] is JobType.WATERMARK
    assert jobs.status == "success"
    assert await context.get_state() is None
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_logo_watermark_on_a_video_through_the_router(assets, tmp_path, clean_env,
                                                              jobs, file_server, session):
    source = assets("silent_video")
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "video/mp4", size=400 * MB)
    message.keep = tmp_path / "received"
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    file_server.source = assets("logo_png")
    await walk_to_logo_confirmation(message, context, settings, bot, assets("logo_png"))

    file_server.source = source
    await watermark_router.apply_watermark(
        FakeCallback(message), context, bot=bot, settings=settings, session=None,
    )

    (document,) = message.documents
    assert document["probe"]["video_codec"] == "h264"
    assert document["probe"]["audio_codecs"] == []
    assert jobs.status == "success", message.answers
    assert bot.downloads == []          # the local Bot API path, not the cloud
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_something_that_is_not_an_image_is_refused_as_a_logo(assets, tmp_path, clean_env,
                                                                   file_server, session):
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(assets("photo"), "image/png")
    bot = FakeBot(SERVER_FILE, size=1000)

    await watermark_router.handle_media(message, context, settings=settings)
    await watermark_router.choose_type(
        FakeCallback(message), WatermarkCallback(action="type", value="logo"), context
    )

    video = as_document(assets("video"), "video/mp4")
    await watermark_router.handle_logo(video, context, bot=bot, settings=settings)

    assert video.answers[-1] == texts.WATERMARK_LOGO_UNSUPPORTED
    # Still waiting for a logo, so the user can simply send a real one.
    assert await context.get_state() == WatermarkStates.waiting_for_logo.state


@needs_ffmpeg
async def test_an_oversized_logo_is_refused_before_it_is_fetched(assets, tmp_path, clean_env,
                                                                 file_server, session):
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(assets("photo"), "image/png")
    bot = FakeBot(SERVER_FILE, size=1000)

    await watermark_router.handle_media(message, context, settings=settings)
    await watermark_router.choose_type(
        FakeCallback(message), WatermarkCallback(action="type", value="logo"), context
    )

    huge = as_document(assets("logo_png"), "image/png", size=5 * MB)
    await watermark_router.handle_logo(huge, context, bot=bot, settings=settings)

    assert huge.answers[-1] == texts.WATERMARK_LOGO_TOO_LARGE
    assert file_server.requested == [], "nothing is fetched for a logo that big"


@needs_ffmpeg
async def test_a_logo_can_be_saved_as_a_preset_and_used_again(assets, tmp_path, clean_env,
                                                              jobs, file_server, session):
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(assets("photo"), "image/png")
    bot = FakeBot(SERVER_FILE, size=1000)

    file_server.source = assets("logo_png")
    await walk_to_logo_confirmation(message, context, settings, bot, assets("logo_png"),
                                    position=Position.TOP_LEFT, size=Size.L, opacity="50")

    callback = FakeCallback(message)
    await watermark_router.save_preset(callback, context, session=session)
    assert callback.answers[-1] == texts.WATERMARK_LOGO_PRESET_SAVED

    (stored,) = await WatermarkPresetsRepository(session).list_for(42)
    assert stored.is_logo and stored.name == "Logo 1"
    assert stored.logo == assets("logo_png").read_bytes()
    assert (stored.position, stored.size, stored.opacity) == ("top_left", "l", 50)

    # A second file, branded from the saved preset alone.
    second = as_document(assets("portrait_photo"), "image/png")
    second_context = fsm_for()
    await watermark_router.handle_media(second, second_context, settings=settings)
    await watermark_router.show_presets(FakeCallback(second), session=session)
    labels = [b.text for row in second.markups[-1].inline_keyboard for b in row]
    assert labels[0] == "🖼 Logo 1"

    await watermark_router.show_preset(
        FakeCallback(second), WatermarkCallback(action="detail", value=str(stored.id)),
        second_context, session=session,
    )
    assert "image / logo" in second.edits[-1]
    detail_labels = [b.text for row in second.markups[-1].inline_keyboard for b in row]
    assert texts.BTN_WATERMARK_EDIT_TEXT not in detail_labels, "a logo has no text to edit"

    await watermark_router.use_preset(
        FakeCallback(second), WatermarkCallback(action="use", value=str(stored.id)),
        second_context, session=session,
    )
    data = await second_context.get_data()
    assert data["kind"] == "logo"
    assert base64.b64decode(data["logo"]) == assets("logo_png").read_bytes()
    assert await second_context.get_state() == WatermarkStates.confirming.state


async def test_a_saved_logo_can_be_renamed_and_deleted(session, assets):
    repository = WatermarkPresetsRepository(session)
    preset = await repository.create(
        42, name="Logo 1", kind="logo", text="", logo=assets("logo_png").read_bytes(),
        logo_format="png", position="bottom_right", style="white_shadow", size="m", opacity=75,
    )

    await repository.rename(preset, "My brand")
    assert (await repository.get(preset.id, 42)).name == "My brand"

    await repository.delete(preset)
    assert await repository.get(preset.id, 42) is None
    assert await repository.list_for(42) == []


# --- batch ---------------------------------------------------------------------------------


@needs_ffmpeg
async def test_a_batch_can_be_branded_with_a_saved_logo(assets, tmp_path, session):
    """The batch processor draws the stored logo on each file in turn."""
    logo = assets("logo_png").read_bytes()
    processor = WatermarkProcessor(
        service(), LogoSpec(position=Position.TOP_RIGHT, size=Size.M, opacity=100),
        logo=logo, logo_format="png",
    )
    assert processor.job_type is JobType.WATERMARK

    for name in ("photo", "silent_video"):
        source = assets(name)
        workspace = workspace_in(tmp_path)
        fetched = SimpleNamespace(path=source)
        incoming = SimpleNamespace(original_filename=source.name)

        output = await processor.run(fetched, workspace, incoming, job_id=None)

        assert output.path is not None and output.size_bytes > 0
        assert output.filename.startswith("atreox_watermarked_")
        if name == "photo":
            assert region_of(changed_box(source, output.path), (1600, 900)) == "top_right"


@needs_ffmpeg
async def test_the_batch_keyboard_offers_saved_logos(session, assets):
    from app.bot.keyboards.batch import watermark_choices

    repository = WatermarkPresetsRepository(session)
    await repository.create(
        42, name="@luna", text="@luna",
        position="bottom_right", style="white_shadow", size="m", opacity=75,
    )
    await repository.create(
        42, name="Logo 1", kind="logo", text="", logo=assets("logo_png").read_bytes(),
        logo_format="png", position="bottom_right", style="white_shadow", size="m", opacity=75,
    )

    presets = await repository.list_for(42)
    labels = [b.text for row in watermark_choices(presets).inline_keyboard for b in row]
    assert labels[:2] == ["💾 @luna", "🖼 Logo 1"]


@needs_ffmpeg
async def test_a_text_watermark_still_works_unchanged(assets, tmp_path):
    """The logo path must not have disturbed the text one."""
    source = assets("photo")
    analysis = await service().analyze(source)
    result = await service().apply(
        source, workspace_in(tmp_path), analysis, WatermarkSpec("@luna")
    )
    box = changed_box(source, result.path)
    assert box is not None
    assert region_of(box, (1600, 900)) == "bottom_right"

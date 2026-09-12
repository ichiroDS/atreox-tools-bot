"""Run every media tool once, inside the container that serves users.

This is the smoke test for a deployment: it proves that the image this bot is
actually running has the codecs, the fonts and the Pillow build each tool
needs - which a passing test suite on a developer's machine does not.

It touches nothing a user owns: no Telegram calls, no database, no reading of
anyone's media. It makes its own tiny sources with FFmpeg and Pillow inside a
temporary directory and deletes that directory when it is done.

From a machine with the Railway CLI::

    railway ssh --service atreox-tools-bot -- python -m scripts.smoke_media

Exit code 0 means every tool worked; 1 names the ones that did not.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.services.media.animation import (  # noqa: E402
    AnimationService,
    ClipChoice,
    plan_clip,
)
from app.services.media.circle import FfmpegVideoCircleService  # noqa: E402
from app.services.media.frame import FrameService, Position  # noqa: E402
from app.services.media.metadata import MetadataService  # noqa: E402
from app.services.media.optimizer import MediaOptimizerService, Preset  # noqa: E402
from app.services.media.probe import MediaProbe  # noqa: E402
from app.services.media.sticker import StickerService, StickerStyle  # noqa: E402
from app.services.media.voice import FfmpegVoiceNoteService  # noqa: E402
from app.services.media.watermark import (  # noqa: E402
    LogoSpec,
    WatermarkService,
    WatermarkSpec,
)
from app.utils.subprocess import run_command  # noqa: E402
from app.utils.temp_files import JobWorkspace  # noqa: E402


async def _sources(root: Path, settings: Settings) -> tuple[Path, Path, Path]:
    """A short video with sound, a photo, and a transparent logo."""
    from PIL import Image, ImageDraw

    video = root / "clip.mp4"
    # Through the same guarded helper every tool uses, so this too is bounded.
    await run_command(
        [settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440",
         "-t", "3", "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)],
        timeout=300,
    )

    photo = root / "photo.png"
    Image.new("RGB", (1200, 800), (18, 18, 24)).save(photo)

    logo = root / "logo.png"
    mark = Image.new("RGBA", (300, 150), (0, 0, 0, 0))
    ImageDraw.Draw(mark).ellipse((0, 0, 299, 149), fill=(230, 40, 40, 255))
    mark.save(logo)
    return video, photo, logo


def _workspace(root: Path) -> JobWorkspace:
    path = root / f"ws-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return JobWorkspace(job_id=uuid.uuid4(), path=path)


async def _run(root: Path, settings: Settings) -> list[tuple[str, bool, str]]:
    video, photo, logo = await _sources(root, settings)
    timeouts = dict(timeout=settings.process_timeout_seconds, probe_timeout=60)
    animation = AnimationService(ffmpeg_bin=settings.ffmpeg_bin,
                                 ffprobe_bin=settings.ffprobe_bin, **timeouts)
    frames = FrameService(ffmpeg_bin=settings.ffmpeg_bin,
                          ffprobe_bin=settings.ffprobe_bin, **timeouts)
    stickers = StickerService(ffprobe_bin=settings.ffprobe_bin, **timeouts)
    watermarks = WatermarkService(ffmpeg_bin=settings.ffmpeg_bin,
                                  ffprobe_bin=settings.ffprobe_bin,
                                  font=settings.watermark_font, **timeouts)
    optimizer = MediaOptimizerService(ffmpeg_bin=settings.ffmpeg_bin,
                                      ffprobe_bin=settings.ffprobe_bin, **timeouts)
    circles = FfmpegVideoCircleService(
        ffmpeg_bin=settings.ffmpeg_bin,
        probe=MediaProbe(settings.ffprobe_bin, timeout=60),
        size=settings.video_note_size,
        max_duration=settings.video_note_max_duration,
        timeout=settings.process_timeout_seconds,
    )
    voices = FfmpegVoiceNoteService(ffmpeg_bin=settings.ffmpeg_bin,
                                    ffprobe_bin=settings.ffprobe_bin, **timeouts)
    metadata = MetadataService(exiftool_bin=settings.exiftool_bin,
                               timeout=settings.process_timeout_seconds)

    async def circle() -> str:
        info = await circles.probe(video)
        segment = circles.plan(info)[0]
        workspace = _workspace(root)
        result = await circles.encode_segment(
            video, workspace.new_file(".mp4", prefix="circle_"), segment, with_audio=info.has_audio
        )
        return f"{settings.video_note_size}px, {result.size_bytes} bytes"

    async def voice() -> str:
        info = await voices.probe(video)
        workspace = _workspace(root)
        result = await voices.encode(video, workspace.new_file(".ogg", prefix="voice_"))
        return f"{result.duration:.1f}s from {info.audio_codec}, {result.size_bytes} bytes"

    async def clean_metadata() -> str:
        workspace = _workspace(root)
        copy = workspace.path / "photo.jpg"
        from PIL import Image

        with Image.open(photo) as picture:
            picture.convert("RGB").save(copy, quality=95)
        result = await metadata.clean(copy)
        return f"{result.size_bytes} bytes"

    async def gif() -> str:
        source = await animation.analyze(video)
        result = await animation.to_gif(video, _workspace(root), source,
                                        plan_clip(source, ClipChoice.FIRST))
        return f"{result.width}x{result.height}, {result.size_bytes} bytes"

    async def mp4() -> str:
        source = await animation.analyze(video)
        result = await animation.to_mp4(video, _workspace(root), source)
        return f"{result.width}x{result.height}, {result.size_bytes} bytes"

    async def optimize_gif() -> str:
        source = await animation.analyze(video)
        made = await animation.to_gif(video, _workspace(root), source, plan_clip(source))
        again = await animation.analyze(made.path)
        result = await animation.optimize_gif(made.path, _workspace(root), again)
        return f"{made.size_bytes} -> {result.size_bytes} bytes"

    async def frame() -> str:
        source = await frames.analyze(video)
        result = await frames.extract(video, _workspace(root), source,
                                      Position.MIDDLE, "jpeg")
        return f"{result.width}x{result.height} at {result.seconds:.2f}s"

    async def sticker() -> str:
        source = await stickers.analyze(logo)
        result = await stickers.make(logo, _workspace(root), source,
                                     StickerStyle.WHITE_OUTLINE)
        return f"{result.width}x{result.height}, {result.size_bytes} bytes"

    async def text_watermark() -> str:
        source = await watermarks.analyze(photo)
        result = await watermarks.apply(photo, _workspace(root), source,
                                        WatermarkSpec("@atreox"))
        return f"{result.width}x{result.height}, {result.size_bytes} bytes"

    async def logo_on_photo() -> str:
        source = await watermarks.analyze(photo)
        result = await watermarks.apply_logo(photo, _workspace(root), source,
                                             LogoSpec(), logo)
        return f"{result.width}x{result.height}, {result.size_bytes} bytes"

    async def logo_on_video() -> str:
        source = await watermarks.analyze(video)
        result = await watermarks.apply_logo(video, _workspace(root), source,
                                             LogoSpec(), logo)
        return f"{result.width}x{result.height}, {result.size_bytes} bytes"

    async def optimize() -> str:
        source = await optimizer.analyze(video)
        result = await optimizer.optimize(video, _workspace(root), source, Preset.BALANCED)
        return f"{result.width}x{result.height}, {result.size_bytes} bytes"

    checks = (
        # The tools this deployment changed, first.
        ("GIF / MP4 - video to GIF", gif),
        ("GIF / MP4 - to MP4", mp4),
        ("GIF / MP4 - optimize GIF", optimize_gif),
        ("Extract Frame", frame),
        ("Make Sticker", sticker),
        ("Watermark - text", text_watermark),
        ("Watermark - logo on a photo", logo_on_photo),
        ("Watermark - logo on a video", logo_on_video),
        ("Media Optimizer", optimize),
        # ...then the ones it did not, because a shared change can still
        # break them.
        ("Video to Circle", circle),
        ("Voice Note", voice),
        ("Metadata Studio - clean", clean_metadata),
    )

    results: list[tuple[str, bool, str]] = []
    for name, check in checks:
        try:
            results.append((name, True, await check()))
        except Exception as error:  # noqa: BLE001 - this is the report
            traceback.print_exc()
            results.append((name, False, f"{type(error).__name__}: {error}"))
    return results


def main() -> int:
    settings = Settings()
    # Alongside the real job workspaces, so this exercises the same disk.
    Path(settings.temp_root).mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="smoke-", dir=settings.temp_root))
    try:
        results = asyncio.run(_run(root, settings))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} tools worked")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

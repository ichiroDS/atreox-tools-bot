"""Turn one image into a static Telegram sticker, in its own process.

Run by :mod:`app.services.media.sticker` as
``python sticker_worker.py REQUEST.json``. Standard library and Pillow only, so
it can be started by file path; a huge or hanging decode takes this process
down rather than the bot, and the caller's timeout can kill it.

What Telegram requires of a static sticker, and what this therefore promises:
WEBP, exactly 512 pixels on the long side, at most 512 KB, transparency kept.
Fully transparent margins are trimmed first, so the subject fills the sticker
instead of floating in empty space.

The outline styles are drawn from the picture's own alpha channel: the mask is
dilated by the outline width and filled with the chosen colour underneath the
image. A picture with no transparency has a rectangular mask, so its outline is
a plain border - which is the honest result for an opaque photo rather than a
guess at where its subject ends.
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, UnidentifiedImageError

# Same directory, so this works when the file is run by path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_worker  # noqa: E402

EXIT_UNSUPPORTED = image_worker.EXIT_UNSUPPORTED
EXIT_TOO_LARGE = image_worker.EXIT_TOO_LARGE
EXIT_CORRUPT = image_worker.EXIT_CORRUPT
# The picture is too small to make a sticker out of.
EXIT_TOO_SMALL = 5

MAX_PIXELS = image_worker.MAX_PIXELS
Unsupported = image_worker.Unsupported
TooManyPixels = image_worker.TooManyPixels


class TooSmall(Exception):
    pass


# Telegram's static sticker box.
STICKER_SIDE = 512
# ...and its file size ceiling.
MAX_STICKER_BYTES = 512 * 1024

# Below this a sticker would be pure upscaling artefacts.
MIN_SOURCE_SIDE = 32

# Outline thickness at 512 px, chosen to read at sticker size without eating
# the picture.
OUTLINE_WIDTH = 12

OUTLINE_COLOURS = {"white_outline": (255, 255, 255), "black_outline": (0, 0, 0)}

# Tried in order until the file fits under the size ceiling.
_QUALITY_LADDER = (92, 85, 75, 65, 55, 45)


def _trim_transparent(image: Image.Image) -> Image.Image:
    """Drop fully transparent margins, so the subject fills the sticker."""
    if image.mode != "RGBA":
        return image
    box = image.getchannel("A").getbbox()
    if box is None:
        raise Unsupported("a fully transparent image")
    if box != (0, 0, image.width, image.height):
        return image.crop(box)
    return image


def _fit(width: int, height: int, box: int) -> tuple[int, int]:
    """Scale so the long side is exactly ``box``, keeping the aspect ratio."""
    scale = box / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _outline(image: Image.Image, colour: tuple[int, int, int], width: int) -> Image.Image:
    """The picture over a dilated silhouette of itself, in ``colour``."""
    padded = Image.new("RGBA", (image.width + 2 * width, image.height + 2 * width), (0, 0, 0, 0))
    padded.paste(image, (width, width))

    # MaxFilter grows the opaque area by half its kernel in every direction.
    mask = padded.getchannel("A").filter(ImageFilter.MaxFilter(2 * width + 1))
    # Anything partly covered becomes solid, so the outline has a clean edge.
    mask = mask.point(lambda value: 255 if value > 8 else 0)

    canvas = Image.new("RGBA", padded.size, colour + (0,))
    canvas.putalpha(mask)
    return Image.alpha_composite(canvas, padded)


def _save(image: Image.Image, destination: Path) -> tuple[int, int]:
    """Write the WEBP, stepping quality down until it fits Telegram's limit."""
    for quality in _QUALITY_LADDER:
        image.save(destination, "WEBP", quality=quality, method=4, exact=True)
        size = destination.stat().st_size
        if size <= MAX_STICKER_BYTES:
            return quality, size
    return _QUALITY_LADDER[-1], destination.stat().st_size


def make(request: dict) -> dict:
    source, destination = Path(request["source"]), Path(request["destination"])
    style = str(request.get("style") or "clean")
    image_format = request["format"]
    Image.MAX_IMAGE_PIXELS = max(MAX_PIXELS.values())
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)

    with Image.open(source) as opened:
        if getattr(opened, "n_frames", 1) > 1:
            raise Unsupported("animated image")
        if opened.width * opened.height > MAX_PIXELS.get(image_format, 0):
            raise TooManyPixels(f"{opened.width}x{opened.height}")
        if min(opened.width, opened.height) < MIN_SOURCE_SIDE:
            raise TooSmall(f"{opened.width}x{opened.height}")
        # A sideways phone photo must become an upright sticker.
        image = ImageOps.exif_transpose(opened)
        image = image.convert("RGBA")

    image = _trim_transparent(image)
    had_alpha = image.getchannel("A").getextrema()[0] < 255

    colour = OUTLINE_COLOURS.get(style)
    # The outline is drawn outside the picture, so the picture itself is
    # scaled down by exactly the room the outline will need.
    inner = STICKER_SIDE - 2 * OUTLINE_WIDTH if colour else STICKER_SIDE
    image = image.resize(_fit(image.width, image.height, inner), Image.LANCZOS)
    if colour:
        image = _outline(image, colour, OUTLINE_WIDTH)

    quality, size = _save(image, destination)
    return {
        "width": image.width,
        "height": image.height,
        "alpha": had_alpha,
        "style": style,
        "quality": quality,
        "bytes": size,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: sticker_worker.py REQUEST.json", file=sys.stderr)
        return EXIT_UNSUPPORTED
    try:
        request = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        result = make(request)
    except Unsupported as exc:
        print(f"unsupported: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except (TooManyPixels, Image.DecompressionBombError) as exc:
        print(f"too many pixels: {exc}", file=sys.stderr)
        return EXIT_TOO_LARGE
    except TooSmall as exc:
        print(f"too small: {exc}", file=sys.stderr)
        return EXIT_TOO_SMALL
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, KeyError) as exc:
        print(f"undecodable: {type(exc).__name__}", file=sys.stderr)
        return EXIT_CORRUPT
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

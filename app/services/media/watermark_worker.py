"""Draw a watermark on one photo, in its own process.

Run by :mod:`app.services.media.watermark` as
``python watermark_worker.py REQUEST.json``, where the request carries the
source, destination, text and the pixel-exact layout the service planned.
Standard library and Pillow only, so it can be started by file path; a decode
that is huge or hangs takes this process down rather than the bot, and the
caller's timeout can kill it.

What it promises: the same format, the same number of pixels (a photo stored
rotated is written upright, which swaps width and height), transparency kept,
the colour profile kept, and no personal metadata carried into the output.

The text is drawn through a small mask tile rather than a full-frame overlay:
that is correct alpha compositing for an opaque colour, and it keeps memory to
the picture itself no matter how large the frame is.
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

# Same directory, so this works when the file is run by path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_worker  # noqa: E402

EXIT_UNSUPPORTED = image_worker.EXIT_UNSUPPORTED
EXIT_TOO_LARGE = image_worker.EXIT_TOO_LARGE
EXIT_CORRUPT = image_worker.EXIT_CORRUPT
MAX_PIXELS = image_worker.MAX_PIXELS
BUFFERED_JPEG_PIXELS = image_worker.BUFFERED_JPEG_PIXELS

Unsupported = image_worker.Unsupported
TooManyPixels = image_worker.TooManyPixels

# Modes that can be drawn on directly; anything else is converted first.
_DIRECT_MODES = ("RGB", "RGBA", "L")


def _drawing_mode(image: Image.Image) -> str:
    if image.mode in _DIRECT_MODES:
        return image.mode
    if image.mode in ("P", "PA", "LA", "1"):
        transparent = image.mode in ("PA", "LA") or "transparency" in image.info
        return "RGBA" if transparent else "RGB"
    # CMYK round-trips with shifted colours; 16-bit modes would be flattened.
    raise Unsupported(f"{image.mode} image")


def _text_tile(text: str, font: ImageFont.FreeTypeFont, shadow_offset: int
               ) -> tuple[Image.Image, tuple[int, int]]:
    """An 8-bit coverage mask of the line, plus room for its shadow."""
    left, top, right, bottom = font.getbbox(text)
    width = max(1, right - left + shadow_offset)
    height = max(1, bottom - top + shadow_offset)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).text((-left, -top), text, font=font, fill=255)
    return mask, (width, height)


def _placement(position: str, frame: tuple[int, int], tile: tuple[int, int], padding: int
               ) -> tuple[int, int]:
    frame_width, frame_height = frame
    tile_width, tile_height = tile
    right = max(padding, frame_width - tile_width - padding)
    bottom = max(padding, frame_height - tile_height - padding)
    centre = max(padding, (frame_width - tile_width) // 2)
    return {
        "top_left": (padding, padding),
        "top_right": (right, padding),
        "bottom_left": (padding, bottom),
        "bottom_right": (right, bottom),
        "bottom_center": (centre, bottom),
    }[position]


def _dim(mask: Image.Image, alpha: float) -> Image.Image:
    """Scale a coverage mask by the chosen opacity."""
    if alpha >= 0.999:
        return mask
    return mask.point(lambda value: int(value * alpha))


def _solid(mode: str, size: tuple[int, int], colour: str) -> Image.Image:
    level = 255 if colour == "white" else 0
    fill: object = level
    if mode == "RGB":
        fill = (level, level, level)
    elif mode == "RGBA":
        fill = (level, level, level, 255)
    return Image.new(mode, size, fill)  # type: ignore[arg-type]


def _logo_tile(path: Path, size: tuple[int, int], alpha: float) -> Image.Image:
    """The logo at its planned size, dimmed to the chosen opacity.

    Its own alpha channel is scaled rather than replaced, so a transparent PNG
    stays transparent and nothing gains a background box behind it.
    """
    with Image.open(path) as opened:
        if getattr(opened, "n_frames", 1) > 1:
            raise Unsupported("animated logo")
        logo = opened.convert("RGBA")
    logo = logo.resize((max(1, size[0]), max(1, size[1])), Image.LANCZOS)
    if alpha < 0.999:
        logo.putalpha(_dim(logo.getchannel("A"), alpha))
    return logo


def draw(request: dict) -> dict:
    source, destination = Path(request["source"]), Path(request["destination"])
    image_format = request["format"]
    logo_mode = request.get("mode") == "logo"
    Image.MAX_IMAGE_PIXELS = max(MAX_PIXELS.values())
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)

    with Image.open(source) as opened:
        if getattr(opened, "n_frames", 1) > 1:
            raise Unsupported("animated image")
        if opened.width * opened.height > MAX_PIXELS.get(image_format, 0):
            raise TooManyPixels(f"{opened.width}x{opened.height}")
        if image_format == "png" and image_worker._png_bit_depth(source) == 16:
            raise Unsupported("16-bit colour PNG")
        icc_profile = opened.info.get("icc_profile")
        # Upright pixels: the text must not end up sideways on a phone photo.
        image = ImageOps.exif_transpose(opened)
        image = image.convert(_drawing_mode(image))

    if logo_mode:
        logo = _logo_tile(
            Path(request["logo"]),
            (int(request["logo_width"]), int(request["logo_height"])),
            float(request["alpha"]),
        )
        # The logo's own alpha is the mask, so only its opaque pixels land.
        image.paste(logo.convert(image.mode), (int(request["x"]), int(request["y"])),
                    logo.getchannel("A"))
    else:
        font = ImageFont.truetype(request["font"], int(request["font_size"]))
        mask, tile = _text_tile(request["text"], font, int(request["shadow_offset"]))
        x, y = _placement(request["position"], image.size, tile, int(request["padding"]))

        if request.get("shadow"):
            shadow = Image.new("L", tile, 0)
            offset = int(request["shadow_offset"])
            shadow.paste(mask, (offset, offset))
            image.paste(_solid(image.mode, tile, "black"), (x, y),
                        _dim(shadow, float(request["shadow_alpha"])))
        image.paste(_solid(image.mode, tile, request["colour"]), (x, y),
                    _dim(mask, float(request["alpha"])))

    _save(image, destination, image_format, source, icc_profile)
    return {
        "width": image.width,
        "height": image.height,
        "mode": image.mode,
        "alpha": "A" in image.getbands(),
    }


def _save(image: Image.Image, destination: Path, image_format: str, source: Path,
          icc_profile: bytes | None) -> None:
    options: dict = {}
    if icc_profile:
        options["icc_profile"] = icc_profile

    if image_format == "jpeg":
        # High quality, and 4:4:4 so the text edges stay crisp. EXIF is not
        # carried over: the pixels are already upright and the rest is personal.
        buffered = image.width * image.height <= BUFFERED_JPEG_PIXELS
        final = image if image.mode == "L" else image.convert("RGB")
        final.save(destination, "JPEG", quality=95, subsampling=0,
                   optimize=buffered, progressive=buffered, **options)
    elif image_format == "png":
        image.save(destination, "PNG", optimize=True, **options)
    else:
        if image_worker._webp_is_lossless(source):
            options.update(lossless=True, quality=80, exact=True)
        else:
            options.update(quality=92)
        image.save(destination, "WEBP", method=4, **options)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: watermark_worker.py REQUEST.json", file=sys.stderr)
        return EXIT_UNSUPPORTED
    try:
        request = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
        result = draw(request)
    except Unsupported as exc:
        print(f"unsupported: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except (TooManyPixels, Image.DecompressionBombError) as exc:
        print(f"too many pixels: {exc}", file=sys.stderr)
        return EXIT_TOO_LARGE
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, KeyError) as exc:
        print(f"undecodable: {type(exc).__name__}", file=sys.stderr)
        return EXIT_CORRUPT
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

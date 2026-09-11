"""Re-encode one image, in its own process.

Run by :mod:`app.services.media.optimizer` as
``python image_worker.py SOURCE DESTINATION FORMAT PRESET``. It imports only the
standard library and Pillow, so it can be started by file path from any working
directory, and a decode that is huge or hangs takes this process down - not the
bot - and is killed by the caller's timeout.

The promise is narrow: the same format, the same pixel dimensions, transparency
kept, and nothing that changes how the picture displays. Only the encoding
changes. EXIF is dropped except for Orientation (so a phone photo stays upright
without its pixels being rotated) and the ICC profile is kept (so colours do
not shift).

The result is printed as one JSON line on stdout. Failures exit with a code
the caller maps to user copy: 2 unsupported, 3 too many pixels, 4 undecodable.
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

from PIL import Image, ImageChops, JpegImagePlugin, UnidentifiedImageError

EXIT_UNSUPPORTED = 2
EXIT_TOO_LARGE = 3
EXIT_CORRUPT = 4

# Largest image per format, so one encode stays near 400 MB inside the bot's
# 1 GB container (measured: JPEG ~4, PNG ~5, WEBP ~24 bytes per pixel).
# Checked from the header, before anything is decoded. Kept equal to
# optimizer.IMAGE_PIXEL_LIMITS, which refuses such files at analysis already.
MAX_PIXELS = {"jpeg": 50_000_000, "png": 50_000_000, "webp": 16_000_000}


class TooManyPixels(Exception):
    pass


# Largest JPEG written optimised/progressive; see _save_jpeg.
BUFFERED_JPEG_PIXELS = 24_000_000

ORIENTATION_TAG = 0x0112

# Highest JPEG quality per preset. "keep" re-uses the source's own quantisation
# tables, so High Quality re-encodes without adding generation loss.
JPEG_QUALITY = {"small": 70, "balanced": 82, "high": "keep"}
# Chroma subsampling: 2 = 4:2:0; "keep" = whatever the source used.
JPEG_SUBSAMPLING = {"small": 2, "balanced": "keep", "high": "keep"}
WEBP_QUALITY = {"small": 70, "balanced": 80, "high": 90}

# The IJG base luminance table: comparing a file's table against it gives the
# quality the file was saved at. Order does not matter - only the sum is used.
_IJG_LUMINANCE = (
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
)


class Unsupported(Exception):
    pass


def estimate_jpeg_quality(image: Image.Image) -> int | None:
    """The IJG quality a JPEG was most likely saved with, or ``None``."""
    tables = getattr(image, "quantization", None) or {}
    luminance = tables.get(0)
    if not luminance or len(luminance) != 64:
        return None
    scale = sum(luminance) * 100.0 / sum(_IJG_LUMINANCE)
    quality = (200.0 - scale) / 2.0 if scale <= 100 else 5000.0 / scale
    return max(1, min(100, round(quality)))


def _orientation_only_exif(image: Image.Image) -> bytes | None:
    orientation = image.getexif().get(ORIENTATION_TAG)
    if not orientation or orientation == 1:
        return None
    exif = Image.Exif()
    exif[ORIENTATION_TAG] = orientation
    return exif.tobytes()


def _webp_is_lossless(path: Path) -> bool:
    """Walk the RIFF chunks for a VP8L (lossless) bitstream; reads headers only."""
    with open(path, "rb") as handle:
        header = handle.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WEBP":
            return False
        for _ in range(64):
            chunk = handle.read(8)
            if len(chunk) < 8:
                return False
            kind, length = chunk[:4], int.from_bytes(chunk[4:], "little")
            if kind == b"VP8L":
                return True
            if kind == b"VP8 ":
                return False
            handle.seek(length + (length & 1), 1)
    return False


def _save_jpeg(image: Image.Image, destination: Path, preset: str) -> dict:
    source_quality = estimate_jpeg_quality(image)
    quality = JPEG_QUALITY[preset]
    subsampling = JPEG_SUBSAMPLING[preset]
    if quality != "keep" and source_quality is not None:
        # Never spend more bits than the source had.
        quality = min(quality, source_quality)
    if subsampling == "keep" and JpegImagePlugin.get_sampling(image) == -1:
        subsampling = 2
    if quality == "keep" and not getattr(image, "quantization", None):
        quality = 90
    # Optimised Huffman tables and progressive scans make libjpeg buffer every
    # coefficient of the image (~5 bytes/pixel on top of the decoded pixels):
    # 50 MP peaked at ~440 MB. Past this size a baseline JPEG is written
    # instead, streamed row by row.
    buffered = image.width * image.height <= BUFFERED_JPEG_PIXELS
    options = dict(quality=quality, subsampling=subsampling, optimize=buffered,
                   progressive=buffered)
    exif = _orientation_only_exif(image)
    if exif:
        options["exif"] = exif
    if image.info.get("icc_profile"):
        options["icc_profile"] = image.info["icc_profile"]
    image.save(destination, "JPEG", **options)
    return {"source_quality": source_quality, "quality": quality}


def _exact_palette(image: Image.Image) -> Image.Image | None:
    """The same pixels as an 8-bit palette image, if they fit in 256 colours.

    Verified pixel for pixel, so it is only ever a lossless change.
    """
    if image.mode != "RGB" or "transparency" in image.info:
        return None
    colours = image.getcolors(256)
    if colours is None:
        return None
    palette = Image.new("P", (1, 1))
    flat: list[int] = []
    for _, colour in colours:
        flat.extend(colour)
    palette.putpalette(flat + [0] * (768 - len(flat)))
    # Mapping a pixel to a palette index depends only on its colour, so it is
    # proven exact on a 1xN strip of the distinct colours - not by diffing
    # full-size copies (which cost ~600 MB at 48 MP).
    strip = Image.new("RGB", (len(colours), 1))
    strip.putdata([colour for _, colour in colours])
    mapped = strip.quantize(palette=palette, dither=Image.Dither.NONE)
    if mapped.tobytes() != bytes(range(len(colours))):
        return None
    return image.quantize(palette=palette, dither=Image.Dither.NONE)


def _png_bit_depth(path: Path) -> int:
    """Bit depth from the IHDR chunk (always first, at a fixed offset)."""
    with open(path, "rb") as handle:
        header = handle.read(25)
    return header[24] if len(header) == 25 and header[12:16] == b"IHDR" else 8


def _save_png(image: Image.Image, source: Path, destination: Path) -> dict:
    """Lossless for every preset: the decoded pixels never change."""
    if _png_bit_depth(source) == 16 and image.mode in ("RGB", "RGBA", "LA"):
        # Pillow would read these as 8-bit. The service sends them to FFmpeg,
        # which keeps all 16 bits; refusing here is the safety net.
        raise Unsupported("16-bit colour PNG")
    icc_profile = image.info.get("icc_profile")
    transparency = image.info.get("transparency")
    reduced = "none"
    if image.mode == "RGBA" and image.getchannel("A").getextrema() == (255, 255):
        # An alpha channel that is opaque everywhere carries no transparency.
        image = image.convert("RGB")
        reduced = "opaque_alpha"
    palette = _exact_palette(image)
    if palette is not None:
        image = palette
        reduced = "palette"
    options: dict = dict(optimize=True)
    if icc_profile:
        options["icc_profile"] = icc_profile
    if transparency is not None and reduced == "none":
        # A palette or colour-key transparency lives in info, not in the pixels.
        options["transparency"] = transparency
    image.save(destination, "PNG", **options)
    return {"reduced": reduced}


def _save_webp(image: Image.Image, source: Path, destination: Path, preset: str) -> dict:
    lossless = _webp_is_lossless(source)
    if lossless:
        # Lossless stays lossless. Quality and method are effort here, not
        # fidelity; the top settings cost many times the time for ~1%.
        options: dict = dict(lossless=True, quality=80, method=4, exact=True)
    else:
        # Method 4 halves the time of 6 at the same memory and near-same size.
        options = dict(quality=WEBP_QUALITY[preset], method=4)
    if image.info.get("icc_profile"):
        options["icc_profile"] = image.info["icc_profile"]
    image.save(destination, "WEBP", **options)
    return {"lossless": lossless}


def optimize(source: Path, destination: Path, fmt: str, preset: str) -> dict:
    # Pillow only *raises* at twice MAX_IMAGE_PIXELS (it merely warns at 1x),
    # so the budget is enforced explicitly below; this is the backstop.
    Image.MAX_IMAGE_PIXELS = max(MAX_PIXELS.values())
    warnings.simplefilter("ignore", Image.DecompressionBombWarning)
    with Image.open(source) as image:
        if image.width * image.height > MAX_PIXELS.get(fmt, 0):
            raise TooManyPixels(f"{image.width}x{image.height}")
        if getattr(image, "n_frames", 1) > 1:
            raise Unsupported("animated image")
        expected = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}[fmt]
        if image.format != expected:
            raise Unsupported(f"{image.format} is not {expected}")
        if fmt == "jpeg" and image.mode not in ("RGB", "L"):
            # CMYK/YCCK JPEGs round-trip with inverted or shifted colours.
            raise Unsupported(f"{image.mode} JPEG")
        image.load()
        size = image.size
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        if fmt == "jpeg":
            details = _save_jpeg(image, destination, preset)
        elif fmt == "png":
            details = _save_png(image, source, destination)
        else:
            details = _save_webp(image, source, destination, preset)
    return {"width": size[0], "height": size[1], "alpha": has_alpha, **details}


def main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[3] not in ("jpeg", "png", "webp") or argv[4] not in JPEG_QUALITY:
        print("usage: image_worker.py SOURCE DESTINATION jpeg|png|webp small|balanced|high",
              file=sys.stderr)
        return EXIT_UNSUPPORTED
    try:
        result = optimize(Path(argv[1]), Path(argv[2]), argv[3], argv[4])
    except Unsupported as exc:
        print(f"unsupported: {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except (TooManyPixels, Image.DecompressionBombError) as exc:
        print(f"too many pixels: {exc}", file=sys.stderr)
        return EXIT_TOO_LARGE
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        print(f"undecodable: {type(exc).__name__}", file=sys.stderr)
        return EXIT_CORRUPT
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

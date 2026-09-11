"""Every user-facing string lives here so copy changes never touch handlers."""

from __future__ import annotations

# --- Brand ------------------------------------------------------------------
ATREOX_URL = "https://atreoxai.com"

# --- Menu -------------------------------------------------------------------
BTN_CIRCLE = "\U0001f3a5 Video \u2192 Circle"
BTN_VOICE = "\U0001f399 Voice Note"
BTN_OPTIMIZE = "\U0001f5dc Media Optimizer"
BTN_METADATA = "\U0001f9f9 Metadata Studio"
BTN_STICKERS = "\U0001f3ad Find Stickers"
BTN_GROW = "\U0001f680 Grow My Channel"
BTN_HELP = "\u2139\ufe0f Help"
BTN_PRIVACY = "\U0001f512 Privacy"
BTN_ATREOX = "\U0001f680 AtreoxAI"
BTN_BACK = "\u2190 Back"
BTN_MAIN_MENU = "\U0001f3e0 Main Menu"
# Alias kept so call sites that read better as "home" still work.
BTN_MAIN_MENU_HOME = BTN_MAIN_MENU
BTN_CANCEL = "\u274c Cancel"

START = (
    "\u26a1 <b>Atreox Tools</b>\n\n"
    "Free tools for Telegram creators.\n\n"
    "Turn videos into native circles, manage media metadata and discover "
    "sticker packs.\n\n"
    "Choose a tool below \U0001f447"
)

MAIN_MENU = "Choose a tool below \U0001f447"

HELP = (
    "\u2139\ufe0f <b>Atreox Tools</b>\n\n"
    "\U0001f3a5 <b>Video \u2192 Circle</b>\n"
    "Turns a regular video into a native Telegram video circle.\n\n"
    "🎙 <b>Voice Note</b>\n"
    "Turns audio or the audio track from a video into a native Telegram voice "
    "message.\n\n"
    "\U0001f5dc <b>Media Optimizer</b>\n"
    "Reduces photo and video file sizes while keeping them suitable for "
    "Telegram.\n\n"
    "\U0001f9f9 <b>Metadata Studio</b>\n"
    "Clean metadata or apply a custom content profile to media.\n\n"
    "\U0001f3ad <b>Find Stickers</b>\n"
    "Discover useful Telegram sticker packs by category.\n\n"
    "\U0001f4ce <b>Tip:</b>\n"
    "For Metadata Studio, send media as a File/Document whenever possible to "
    "preserve quality."
)

# Every claim here is checked against what the code actually does:
# job workspaces are deleted in a finally block, the wizard keeps its location
# in FSM memory only, and the database stores feature events plus the job
# bookkeeping described below.
PRIVACY = (
    "\U0001f512 <b>Privacy</b>\n\n"
    "Media files are processed temporarily and are not intentionally stored "
    "after processing. Each job works in its own temporary folder that is "
    "deleted once the job finishes, whether it succeeded or failed.\n\n"
    "Locations chosen in Metadata Studio are kept only for the active "
    "processing session and are not stored as permanent user profile data.\n\n"
    "Basic usage data may be stored to understand which tools are used and "
    "improve the service: your Telegram id, username, first name and language, "
    "how you first found the bot, which tools you open, and for each job its "
    "type, result, original file name and file size. "
    "File contents are never stored."
)

# --- AtreoxAI call to action ------------------------------------------------
# Deliberately rare - see app/services/cta.py for the pacing rules.
CTA = (
    "\U0001f680 <b>Need more traffic for your Telegram channel?</b>\n\n"
    "AtreoxAI automates Telegram channel growth."
)
BTN_CTA = "\U0001f680 Explore AtreoxAI"
CTA_OPEN = "\U0001f680 <b>AtreoxAI</b>\n\nTap below to open it \U0001f447"
BTN_CTA_OPEN = "\U0001f680 Open atreoxai.com"

# --- Circle -----------------------------------------------------------------
CIRCLE_PROMPT = (
    "🎥 Send me a video and I'll turn it into a native Telegram video circle.\n\n"
    "For the best quality, send the original video as a File whenever possible."
)
CIRCLE_PROCESSING = "⏳ Turning your video into a circle…"
CIRCLE_DONE = (
    "✅ Your circle is ready.\n\n"
    "💡 When forwarding it, you can hide that it came from a bot.\n\n"
    "Use the button below to see how."
)
CIRCLE_WRONG_INPUT = (
    "Please send a video (or a video file). "
    "Use 🏠 Main Menu to pick another tool."
)

# Long videos: offered a choice instead of being silently trimmed.
BTN_CIRCLE_SPLIT = "✂️ Split into Circles"
BTN_CIRCLE_FIRST = "▶️ First Circle Only"
CIRCLE_CHOICE_EXPIRED = "This choice has expired. Please send the video again."


def format_duration(seconds: float) -> str:
    """``1:01``, ``2:30`` or ``1:02:03`` - how a video player shows it."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def circle_long_video(duration_seconds: float) -> str:
    return (
        f"🎥 This video is {format_duration(duration_seconds)} long.\n\n"
        "What should I do?"
    )


def circle_progress(index: int, total: int) -> str:
    return f"⏳ Sending circle {index}/{total}…"


def circle_partial_failure(sent: int, total: int) -> str:
    return (
        f"⚠️ I sent {sent} of {total} circles, then something went wrong with "
        "the rest. Please try again, or send a shorter video."
    )

BTN_CIRCLE_FORWARD_HELP = '🤖 Remove "Forwarded from bot"'
BTN_CIRCLE_AGAIN = "🎥 Make Another Circle"
BTN_GUIDE_IOS = "🍎 iOS"
BTN_GUIDE_ANDROID = "🤖 Android"

# The steps are identical on every platform today. They live in one place so a
# platform guide can diverge (or gain a GIF) without touching the others.
def _forward_help_steps(item: str) -> str:
    return (
        f"1. Press and hold the {item} → Forward\n"
        "2. Select the chat/channel\n"
        "3. Open Message Settings / Forwarding Options\n"
        "4. Choose Hide Sender Name\n"
        "5. Send\n\n"
        "The exact wording may differ slightly between iOS, Android and Desktop."
    )


_FORWARD_HELP_TITLE = "🤖 <b>How to hide the bot name when forwarding</b>\n\n"
_FORWARD_HELP_STEPS = _forward_help_steps("circle")

CIRCLE_FORWARD_HELP = _FORWARD_HELP_TITLE + _FORWARD_HELP_STEPS
CIRCLE_FORWARD_HELP_IOS = "🍎 <b>iOS</b>\n\n" + _FORWARD_HELP_STEPS
CIRCLE_FORWARD_HELP_ANDROID = (
    "🤖 <b>Android</b>\n\n" + _FORWARD_HELP_STEPS
)

# --- Voice note -------------------------------------------------------------
VOICE_PROMPT = (
    "🎙 Send me an audio file or a video.\n\n"
    "I'll turn its audio into a native Telegram voice message.\n\n"
    "You can send large files too."
)
VOICE_PROCESSING = "⏳ Turning it into a voice message…"
VOICE_DONE = "✅ Voice note ready."
BTN_VOICE_AGAIN = "🎙 Make Another"
BTN_VOICE_FORWARD_HELP = '🤖 Hide "Forwarded from bot"'

VOICE_UNSUPPORTED = (
    "⚠️ I can't make a voice message from that. Send an audio file (MP3, WAV, "
    "M4A, OGG, FLAC…) or a video with sound — as media or as a File."
)
VOICE_NO_AUDIO = "🔇 This video doesn't contain an audio track."
VOICE_CORRUPT = (
    "⚠️ I couldn't read the audio in this file. It may be damaged. "
    "Please try another file."
)
VOICE_TIMEOUT = (
    "⏱ This file took too long to convert. Please try a shorter one."
)
VOICE_SEND_FAILED = (
    "⚠️ Telegram didn't accept the voice message. Please try again in a moment."
)
VOICE_FORBIDDEN = (
    "🔒 Your Telegram privacy settings don't allow voice messages from this bot.\n\n"
    "Allow them in Settings → Privacy and Security → Voice Messages, then send "
    "the file again."
)

_VOICE_FORWARD_HELP_STEPS = _forward_help_steps("voice message")
VOICE_FORWARD_HELP = _FORWARD_HELP_TITLE + _VOICE_FORWARD_HELP_STEPS
VOICE_FORWARD_HELP_IOS = "🍎 <b>iOS</b>\n\n" + _VOICE_FORWARD_HELP_STEPS
VOICE_FORWARD_HELP_ANDROID = "🤖 <b>Android</b>\n\n" + _VOICE_FORWARD_HELP_STEPS

# --- Media optimizer --------------------------------------------------------
OPTIMIZE_PROMPT = (
    "🗜 Send me a photo or video.\n\n"
    "I'll reduce the file size while keeping it suitable for Telegram.\n\n"
    "Large files are supported.\n\n"
    "📎 For the best result, send it as a File — Telegram compresses normal "
    "photos and videos before I receive them."
)
OPTIMIZE_ANALYZING = "🔎 Analyzing your file..."
OPTIMIZE_PROCESSING = "⏳ Optimizing your media..."
OPTIMIZE_CHOOSE = "Choose optimization:"
OPTIMIZE_PNG_NOTE = "PNG is optimized losslessly: every pixel and any transparency stay exactly as they are."
OPTIMIZE_COMPRESSED_NOTE = (
    "📎 Telegram already compressed this upload. Send the original as a File "
    "for a better result."
)
OPTIMIZE_ALREADY_OPTIMIZED = "✅ This file is already well optimized."
OPTIMIZE_ALREADY_COMPRESSED = "✅ Your original file is already efficiently compressed."
OPTIMIZE_CHOICE_EXPIRED = "This choice has expired. Please send the file again."

BTN_OPTIMIZE_SMALL = "⚡ Small"
BTN_OPTIMIZE_BALANCED = "⚖️ Balanced"
BTN_OPTIMIZE_HIGH = "💎 High Quality"
BTN_OPTIMIZE_AGAIN = "🗜 Optimize Another"

OPTIMIZE_UNSUPPORTED = (
    "⚠️ I can't optimize that. Send a photo (JPEG, PNG, WEBP) or a video "
    "(MP4, MOV, WEBM, MKV…) — as media or as a File."
)
OPTIMIZE_CORRUPT = (
    "⚠️ I couldn't read this file. It may be damaged. Please try another one."
)
OPTIMIZE_TIMEOUT = (
    "⏱ This file took too long to optimize. Try ⚡ Small, or a shorter video."
)
OPTIMIZE_VERIFY_FAILED = (
    "⚠️ The optimized file didn't pass my checks, so I didn't send it. "
    "Please try another preset."
)
OPTIMIZE_DISK_FULL = (
    "⚠️ The server is short on disk space right now. "
    "Please try again in a few minutes."
)
OPTIMIZE_OUT_OF_MEMORY = (
    "⚠️ This file needed more memory than the server could spare. "
    "Please try again in a few minutes."
)
OPTIMIZE_SEND_FAILED = (
    "⚠️ Telegram didn't accept the file. Please try again in a moment."
)

_KB = 1024
_MB = 1024 * 1024
_GB = 1024 * _MB

_CODEC_NAMES = {
    "h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "vp8": "VP8",
    "mpeg4": "MPEG-4", "prores": "ProRes", "mjpeg": "MJPEG",
}


def format_size(size_bytes: int) -> str:
    """``850 KB``, ``4.2 MB``, ``148 MB``, ``1.4 GB``."""
    if size_bytes < _MB:
        return f"{max(1, round(size_bytes / _KB))} KB"
    if size_bytes < 10 * _MB:
        return f"{size_bytes / _MB:.1f} MB"
    if size_bytes < 1000 * _MB:
        return f"{round(size_bytes / _MB)} MB"
    return f"{size_bytes / _GB:.1f} GB"


def format_clock(seconds: float) -> str:
    """``01:42`` or ``1:02:03``."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_rate(frame_rate: float) -> str:
    if abs(frame_rate - round(frame_rate)) < 0.01:
        return f"{round(frame_rate)} fps"
    return f"{frame_rate:.2f} fps"


def _format_bitrate(bits_per_second: int) -> str:
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.1f} Mbps"
    return f"{max(1, round(bits_per_second / 1000))} kbps"


def video_summary(
    *,
    width: int,
    height: int,
    duration: float,
    size_bytes: int,
    codec: str,
    frame_rate: float,
    bitrate: int,
    has_audio: bool,
) -> str:
    details = [_CODEC_NAMES.get(codec, codec.upper())]
    if frame_rate:
        details.append(_format_rate(frame_rate))
    if bitrate:
        details.append(_format_bitrate(bitrate))
    details.append("with audio" if has_audio else "no audio")
    return (
        "🎬 <b>Video detected</b>\n"
        f"{width}×{height} • {format_clock(duration)} • {format_size(size_bytes)}\n"
        + " • ".join(details)
    )


def image_summary(*, width: int, height: int, image_format: str, size_bytes: int) -> str:
    return (
        "🖼 <b>Photo detected</b>\n"
        f"{width}×{height} • {image_format.upper()} • {format_size(size_bytes)}"
    )


def preset_button(label: str, estimated_bytes: int | None) -> str:
    if estimated_bytes is None:
        return label
    return f"{label} — ~{format_size(estimated_bytes)}"


def optimize_progress(percent: int) -> str:
    return f"{OPTIMIZE_PROCESSING} {percent}%"


def optimize_done(before_bytes: int, after_bytes: int) -> str:
    saved = max(0, round((1 - after_bytes / before_bytes) * 100)) if before_bytes else 0
    return (
        "✅ Optimized\n\n"
        f"Before: {format_size(before_bytes)}\n"
        f"After: {format_size(after_bytes)}\n"
        f"Saved: {saved}%"
    )

# --- Metadata ---------------------------------------------------------------
BTN_METADATA_CLEAN = "🧼 Clean Metadata"
BTN_METADATA_CHANGE = "✏️ Change Metadata"
BTN_METADATA_CLEAN_AGAIN = "🧼 Clean Another"

METADATA_MENU = "🧹 <b>Metadata Studio</b>\n\nWhat would you like to do?"

# Shown before every upload: Telegram silently re-encodes non-document media,
# which is the single biggest cause of disappointing results here.
SEND_AS_FILE_NOTICE = (
    "📎 Send your photo or video as a File/Document whenever possible.\n\n"
    "Telegram may compress normal photos/videos and can alter metadata before "
    "the bot receives them. Sending as a File preserves the original resolution "
    "and source file much better."
)

METADATA_CLEAN_PROMPT = "🧼 <b>Clean Metadata</b>\n\n" + SEND_AS_FILE_NOTICE
METADATA_CHANGE_PROMPT = "✏️ <b>Change Metadata</b>\n\n" + SEND_AS_FILE_NOTICE
METADATA_WRONG_INPUT = (
    "Please send a photo or a video. For best results attach it as a File."
)
METADATA_PROCESSING = "⏳ Working on your file…"
METADATA_CLEAN_DONE = "✅ Metadata cleaned"
METADATA_CHANGE_DONE = "✅ Metadata applied"

METADATA_COMPRESSED_WARNING = (
    "⚠️ Telegram compressed this upload, so some original metadata may already "
    "be gone. Send it as a File for a better result."
)

DEVICE_GENERATION_PROMPT = (
    "\U0001f4f1 <b>Device</b>\n\nWhich iPhone should this file claim to come from?"
)


def device_model_prompt(generation: str) -> str:
    return f"\U0001f4f1 <b>iPhone {generation}</b>\n\nWhich model?"


COUNTRY_PROMPT = "\U0001f30d <b>Location</b>\n\nWhich country?"


def city_prompt(country: str) -> str:
    return f"{country}\n\nWhich city?"


TIME_OF_DAY_PROMPT = "🕒 What time of day should the capture time fall into?"

# Emoji + name per TimeOfDay value. Buttons and the confirmation summary are
# both built from this, so the wording can never drift between them.
TIME_OF_DAY_PARTS: dict[str, tuple[str, str]] = {
    "morning": ("🌅", "Morning"),
    "day": ("☀️", "Day"),
    "evening": ("🌆", "Evening"),
    "night": ("🌙", "Night"),
}


def time_of_day_button(value: str) -> str:
    emoji, name = TIME_OF_DAY_PARTS[value]
    return f"{emoji} {name}"


BTN_APPLY = "✅ Apply Metadata"
BTN_CHANGE_DEVICE = "📱 Change Device"
BTN_CHANGE_LOCATION = "📍 Change Location"
BTN_CHANGE_TIME = "🕐 Change Time"


def confirmation(device: str, city: str, country: str, time_of_day: str) -> str:
    emoji, name = TIME_OF_DAY_PARTS[time_of_day]
    return (
        "\u270f\ufe0f <b>New metadata</b>\n\n"
        f"\U0001f4f1 Device: {device}\n"
        f"\U0001f4cd Location: {city}, {country}\n"
        f"{emoji} Time: {name}\n\n"
        "Apply?"
    )


# --- Stickers ---------------------------------------------------------------
STICKERS_PROMPT = (
    "🎭 <b>Sticker Finder</b>\n\n"
    "Pick a mood. You'll get stickers from different packs — tap any sticker to "
    "open its pack and add it."
)
STICKERS_RESULT_CONTROLS = "Want more?"
STICKERS_EMPTY = (
    "No stickers in this category yet. Try another one — we're adding packs "
    "regularly."
)
BTN_MORE = "🔄 More"
BTN_CATEGORIES = "📂 Categories"

# --- Errors -----------------------------------------------------------------
ERROR_UNSUPPORTED = "That file type isn't supported yet."
ERROR_PROCESSING = (
    "Something went wrong while processing this file. Please try another file."
)
ERROR_TOO_LARGE = "⚠️ This file is too large for the current service limit."
ERROR_OUTPUT_TOO_LARGE = (
    "⚠️ The processed file is too large to send back through Telegram, so it "
    "was not sent. Please try a smaller file."
)
ERROR_GENERIC = "Something went wrong. Let's start over."
SERVER_BUSY = (
    "⏳ The server is busy with other large files right now. "
    "Please try again in a few minutes."
)
USER_JOB_RUNNING = (
    "⏳ Your previous file is still being processed. "
    "Send the next one as soon as it's done."
)
CANCELLED = "Cancelled."


def stats_report(stats) -> str:
    """Admin-only summary. Plain counters, no personal data."""
    lines = [
        "<b>Atreox Tools — Stats</b>",
        "",
        "👥 <b>Users:</b>",
        f"Today: {stats.users.today}",
        f"7d: {stats.users.week}",
        f"Total: {stats.users.total}",
        "",
        "⚙️ <b>Jobs:</b>",
        f"Today: {stats.jobs.today}",
        f"7d: {stats.jobs.week}",
        f"Total: {stats.jobs.total}",
        "",
        f"🎥 Circles: {stats.circles}",
        f"🎙 Voice notes: {stats.voice_notes}",
        f"🗜 Optimizations: {stats.optimizations}",
        f"🧹 Metadata cleans: {stats.metadata_cleans}",
        f"✏️ Metadata changes: {stats.metadata_changes}",
        f"🎭 Sticker searches: {stats.sticker_searches}",
        "",
        f"🚀 Atreox CTA clicks: {stats.cta_clicks}",
        "",
        "<b>Top acquisition sources:</b>",
    ]
    if stats.top_sources:
        lines += [
            f"{index}. {source} — {count}"
            for index, (source, count) in enumerate(stats.top_sources, start=1)
        ]
    else:
        lines.append("No attributed users yet.")
    return "\n".join(lines)


RATE_LIMITED = (
    "\u23f3 You're using the tools very quickly. Wait a moment and try again."
)

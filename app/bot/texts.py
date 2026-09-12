"""Every user-facing string lives here so copy changes never touch handlers."""

from __future__ import annotations

# --- Brand ------------------------------------------------------------------
ATREOX_URL = "https://atreoxai.com"

# --- Menu -------------------------------------------------------------------
BTN_CIRCLE = "\U0001f3a5 Video \u2192 Circle"
BTN_VOICE = "\U0001f399 Voice Note"
BTN_OPTIMIZE = "\U0001f5dc Media Optimizer"
BTN_WATERMARK = "\U0001f5bc Watermark"
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
BTN_BATCH = "\U0001f4e6 Batch Mode"
BTN_ANIMATION = "\U0001f39e GIF / MP4"
BTN_FRAME = "\U0001f5bc Extract Frame"
BTN_MAKE_STICKER = "\U0001f3f7 Make Sticker"

START = (
    "\u26a1 <b>Atreox Tools</b>\n\n"
    "Free tools for Telegram creators.\n\n"
    "Make circles and voice messages, clean or change metadata, shrink files, "
    "add your watermark, or run a whole batch at once.\n\n"
    "Choose a tool below \U0001f447"
)

MAIN_MENU = "Choose a tool below \U0001f447"

# The order follows the main menu, so the two screens teach the same map.
HELP = (
    "\u2139\ufe0f <b>Atreox Tools</b>\n\n"
    "\U0001f3a5 <b>Video \u2192 Circle</b>\n"
    "Turns a regular video into a native Telegram video circle. A long "
    "video can be split into consecutive circles.\n\n"
    "\U0001f399 <b>Voice Note</b>\n"
    "Turns audio, or the audio track from a video, into a native Telegram "
    "voice message.\n\n"
    "\U0001f9f9 <b>Metadata Studio</b>\n"
    "Cleans metadata, or writes a custom device, location and capture "
    "time.\n\n"
    "\U0001f5dc <b>Media Optimizer</b>\n"
    "Reduces photo and video file sizes while keeping them suitable for "
    "Telegram.\n\n"
    "\U0001f5bc <b>Watermark</b>\n"
    "Adds your @username or custom branding text to photos and videos. "
    "Save presets for repeated use.\n\n"
    "\U0001f4e6 <b>Batch Mode</b>\n"
    "Process multiple photos and videos at once with Metadata Cleaner, "
    "Watermark or Media Optimizer.\n\n"
    "\U0001f39e <b>GIF / MP4</b>\n"
    "Turns a video into a GIF, or a GIF into a silent MP4. Long videos are "
    "cut to a short clip you choose.\n\n"
    "\U0001f5bc <b>Extract Frame</b>\n"
    "Saves a single still from a video as a JPG or PNG, at full frame size.\n\n"
    "\U0001f3f7 <b>Make Sticker</b>\n"
    "Turns an image into a real Telegram sticker, with an optional white or "
    "black outline.\n\n"
    "\U0001f3ad <b>Find Stickers</b>\n"
    "Discover useful Telegram sticker packs by category.\n\n"
    "\U0001f4ce <b>Tip:</b>\n"
    "Send photos and videos as a File whenever the original quality "
    "matters - Telegram compresses normal uploads before the bot "
    "receives them.\n\n"
    "Large files are supported."
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
    "Watermarks you choose to save as presets are stored with your Telegram id "
    "until you delete them. A saved image/logo preset keeps that small logo "
    "image itself, so it survives restarts and is there next time; it is "
    "visible only to you and is removed when you delete the preset. Nothing "
    "else you send is kept.\n\n"
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

# --- Watermark --------------------------------------------------------------
WATERMARK_PROMPT = (
    "🖼 Send me a photo or video.\n\n"
    "I'll add your @username or custom text as a clean watermark.\n\n"
    "For best quality, send media as a File."
)
WATERMARK_ASK_TEXT = "What should the watermark say?"
WATERMARK_TYPE_PROMPT = (
    "✏️ Send the watermark text.\n\n"
    "For example: <code>@username</code>, <code>t.me/username</code> or your name.\n\n"
    "One line, up to 48 characters."
)
WATERMARK_TEXT_REJECTED = (
    "⚠️ That won't work as a watermark. Send one short line — up to 48 "
    "characters, with at least one letter or number."
)
WATERMARK_POSITION_PROMPT = "Choose position:"
WATERMARK_STYLE_PROMPT = "Choose style:"
WATERMARK_SIZE_PROMPT = "Watermark size:"
WATERMARK_OPACITY_PROMPT = "Opacity:"
WATERMARK_PROCESSING = "⏳ Adding your watermark..."
WATERMARK_DONE = "✅ Watermark added."
WATERMARK_CHOICE_EXPIRED = "This step has expired. Please send the file again."

BTN_WATERMARK_ENTER_TEXT = "✏️ Enter Text"
BTN_WATERMARK_PRESETS = "💾 My Presets"
BTN_WATERMARK_APPLY = "✅ Apply"
BTN_WATERMARK_SAVE_PRESET = "💾 Save as Preset"
BTN_WATERMARK_CHANGE = "✏️ Change"
BTN_WATERMARK_AGAIN = "🖼 Watermark Another"

# Labels for everything the user picks, keyed by the stored value so the
# service's enums and this copy can never drift apart silently.
WATERMARK_POSITION_LABELS = {
    "top_left": "↖️ Top Left",
    "top_right": "↗️ Top Right",
    "bottom_left": "↙️ Bottom Left",
    "bottom_right": "↘️ Bottom Right",
    "bottom_center": "⬇️ Bottom Center",
}
WATERMARK_STYLE_LABELS = {
    "white": "⚪ White",
    "black": "⚫ Black",
    "white_shadow": "✨ White + Shadow",
}
WATERMARK_SIZE_LABELS = {"s": "S", "m": "M", "l": "L"}


def _plain(label: str) -> str:
    """The label without its emoji, for the summary lines."""
    return label.split(" ", 1)[1] if " " in label else label


def watermark_summary(
    *, text: str, position: str, style: str, size: str, opacity: int
) -> str:
    return (
        "🖼 <b>Watermark</b>\n\n"
        f"Text: {text}\n"
        f"Position: {_plain(WATERMARK_POSITION_LABELS[position])}\n"
        f"Style: {_plain(WATERMARK_STYLE_LABELS[style])}\n"
        f"Size: {WATERMARK_SIZE_LABELS[size]}\n"
        f"Opacity: {opacity}%\n\n"
        "Apply?"
    )


def watermark_progress(percent: int) -> str:
    return f"{WATERMARK_PROCESSING} {percent}%"


# --- Watermark presets ------------------------------------------------------
WATERMARK_PRESETS_TITLE = "💾 <b>My Presets</b>\n\nPick one to use or manage."
WATERMARK_PRESETS_EMPTY = "No saved watermarks yet."
BTN_WATERMARK_SAVE_NEW = "➕ Save New"
BTN_WATERMARK_USE = "✅ Use This"
BTN_WATERMARK_RENAME = "✏️ Rename"
BTN_WATERMARK_EDIT_TEXT = "📝 Edit Text"
BTN_WATERMARK_DELETE = "🗑 Delete"
BTN_WATERMARK_DELETE_CONFIRM = "🗑 Yes, delete"

WATERMARK_NEW_PRESET_PROMPT = (
    "➕ Send the watermark text to save, for example <code>@username</code>."
)
WATERMARK_RENAME_PROMPT = "✏️ Send a new name for this preset."
WATERMARK_EDIT_TEXT_PROMPT = "📝 Send the new watermark text."
WATERMARK_PRESET_SAVED = "💾 Saved."
WATERMARK_PRESET_UPDATED = "💾 Updated."
WATERMARK_PRESET_DELETED = "🗑 Deleted."
WATERMARK_PRESET_GONE = "That preset no longer exists."
WATERMARK_PRESET_DUPLICATE = "You already have a preset with that name."


def watermark_preset_limit(maximum: int) -> str:
    return (
        f"You already have {maximum} saved watermarks. "
        "Delete one before saving another."
    )


def watermark_preset_detail(
    *, name: str, text: str, position: str, style: str, size: str, opacity: int
) -> str:
    return (
        f"💾 <b>{name}</b>\n\n"
        f"Text: {text}\n"
        f"Position: {_plain(WATERMARK_POSITION_LABELS[position])}\n"
        f"Style: {_plain(WATERMARK_STYLE_LABELS[style])}\n"
        f"Size: {WATERMARK_SIZE_LABELS[size]}\n"
        f"Opacity: {opacity}%"
    )


def watermark_delete_confirmation(name: str) -> str:
    return f"🗑 Delete <b>{name}</b>?"


WATERMARK_UNSUPPORTED = (
    "⚠️ I can't watermark that. Send a photo (JPEG, PNG, WEBP) or a video "
    "(MP4, MOV, WEBM, MKV…) — as media or as a File."
)
WATERMARK_CORRUPT = (
    "⚠️ I couldn't read this file. It may be damaged. Please try another one."
)
WATERMARK_TIMEOUT = (
    "⏱ This file took too long to watermark. Please try a shorter video."
)
WATERMARK_VERIFY_FAILED = (
    "⚠️ The watermarked file didn't pass my checks, so I didn't send it. "
    "Please try again."
)
WATERMARK_FONT_MISSING = (
    "⚠️ The watermark font is unavailable on the server right now. "
    "Please try again later."
)

# --- Watermark: image / logo ------------------------------------------------
WATERMARK_TYPE_PROMPT_CHOICE = "Choose watermark type:"
BTN_WATERMARK_TYPE_TEXT = "✏️ Text"
BTN_WATERMARK_TYPE_LOGO = "🖼 Image / Logo"

WATERMARK_LOGO_PROMPT = (
    "🖼 Send me your logo as an image.\n\n"
    "A transparent PNG works best. JPG and WEBP are fine too.\n\n"
    "Keep it small — up to 1 MB."
)
WATERMARK_LOGO_UNSUPPORTED = (
    "⚠️ That doesn't work as a logo. Send a PNG, JPG or WEBP image, up to 1 MB."
)
WATERMARK_LOGO_TOO_LARGE = (
    "⚠️ That logo is too large. Send an image of up to 1 MB — a logo only needs "
    "to be a few hundred pixels wide."
)
WATERMARK_LOGO_GONE = (
    "⚠️ I no longer have that logo. Please send it again."
)
BTN_WATERMARK_LOGO_AGAIN = "🖼 Change Logo"
WATERMARK_LOGO_PRESET_SAVED = "💾 Logo saved to your presets."

# A logo preset is saved in one tap and can be renamed afterwards, so it needs
# a name of its own.
def watermark_logo_preset_name(index: int) -> str:
    return f"Logo {index}"


def watermark_logo_summary(*, position: str, size: str, opacity: int) -> str:
    return (
        "🖼 <b>Logo watermark</b>\n\n"
        f"Position: {_plain(WATERMARK_POSITION_LABELS[position])}\n"
        f"Size: {WATERMARK_SIZE_LABELS[size]}\n"
        f"Opacity: {opacity}%\n\n"
        "Apply?"
    )


def watermark_logo_preset_detail(
    *, name: str, position: str, size: str, opacity: int
) -> str:
    return (
        f"💾 <b>{name}</b>\n\n"
        "Type: image / logo\n"
        f"Position: {_plain(WATERMARK_POSITION_LABELS[position])}\n"
        f"Size: {WATERMARK_SIZE_LABELS[size]}\n"
        f"Opacity: {opacity}%"
    )


# --- GIF / MP4 --------------------------------------------------------------
ANIMATION_PROMPT = (
    "🎞 Send me a video or GIF.\n\n"
    "I can convert video to GIF, or GIF to MP4.\n\n"
    "Large files are supported."
)
ANIMATION_ANALYZING = "🔎 Checking your file..."
ANIMATION_PROCESSING = "⏳ Converting..."
ANIMATION_CHOOSE = "What should I do with it?"
ANIMATION_CUSTOM_TIME_PROMPT = (
    "⌨️ Send the start time, as seconds or mm:ss.\n\n"
    "For example: <code>12</code> or <code>1:05</code>."
)
ANIMATION_TIME_REJECTED = (
    "⚠️ I couldn't read that as a time inside this video. Send seconds "
    "(<code>12</code>) or mm:ss (<code>1:05</code>)."
)
ANIMATION_CHOICE_EXPIRED = "This choice has expired. Please send the file again."

BTN_ANIMATION_TO_GIF = "🎞 Convert to GIF"
BTN_ANIMATION_TO_MP4 = "▶️ Convert to MP4"
BTN_ANIMATION_OPTIMIZE_GIF = "🎞 Optimize GIF"
BTN_ANIMATION_CLIP_FIRST = "✂️ First 6s"
BTN_ANIMATION_CLIP_MIDDLE = "🎯 Middle 6s"
BTN_ANIMATION_CLIP_CUSTOM = "⌨️ Custom Start Time"
BTN_ANIMATION_AGAIN = "🎞 Convert Another"

ANIMATION_GIF_DONE = "✅ Your GIF is ready."
ANIMATION_MP4_DONE = "✅ Your MP4 is ready."
ANIMATION_GIF_ALREADY_SMALL = (
    "✅ This GIF is already efficient — a smaller one would lose quality for "
    "nothing, so I kept your original."
)
ANIMATION_UNSUPPORTED = (
    "⚠️ I can't convert that. Send a video (MP4, MOV, WEBM, MKV…) or a GIF — "
    "as media or as a File."
)
ANIMATION_CORRUPT = (
    "⚠️ I couldn't read this file. It may be damaged. Please try another one."
)
ANIMATION_TIMEOUT = (
    "⏱ This file took too long to convert. Please try a shorter one."
)
ANIMATION_VERIFY_FAILED = (
    "⚠️ The converted file didn't pass my checks, so I didn't send it. "
    "Please try again."
)


def animation_clip_prompt(duration: float, clip_seconds: float) -> str:
    return (
        f"🎞 This video is {format_clock(duration)} long.\n\n"
        f"A GIF should be short, so I'll use {round(clip_seconds)} seconds of it.\n\n"
        "Which part?"
    )


def animation_gif_summary(*, width: int, height: int, frame_rate: float, seconds: float) -> str:
    return (
        "🎞 <b>GIF</b>\n"
        f"{width}×{height} • {round(frame_rate)} fps • {format_clock(seconds)}"
    )


def animation_progress(percent: int) -> str:
    return f"{ANIMATION_PROCESSING} {percent}%"


# --- Make Sticker -----------------------------------------------------------
STICKER_MAKE_PROMPT = (
    "🏷 Send me an image and I'll turn it into a Telegram sticker.\n\n"
    "PNG with transparency works best, but JPG and WEBP are also supported."
)
STICKER_MAKE_STYLE_PROMPT = "Choose a style:"
STICKER_MAKE_PROCESSING = "⏳ Making your sticker..."
STICKER_MAKE_DONE = (
    "✅ Your sticker is ready. Forward it anywhere, or save it to your favourites."
)
STICKER_MAKE_CHOICE_EXPIRED = "This choice has expired. Please send the image again."

BTN_STICKER_CLEAN = "✨ Clean"
BTN_STICKER_WHITE_OUTLINE = "⚪ White Outline"
BTN_STICKER_BLACK_OUTLINE = "⚫ Black Outline"
BTN_STICKER_MAKE_AGAIN = "🏷 Make Another"

STICKER_MAKE_UNSUPPORTED = (
    "⚠️ I can't make a sticker from that. Send a PNG, JPG or WEBP image — as a "
    "photo or as a File."
)
STICKER_MAKE_TOO_SMALL = (
    "⚠️ That image is too small to become a sticker. Send one at least 32 "
    "pixels on each side — a few hundred is better."
)
STICKER_MAKE_CORRUPT = (
    "⚠️ I couldn't read this image. It may be damaged. Please try another one."
)
STICKER_MAKE_SEND_FAILED = (
    "⚠️ Telegram didn't accept the sticker. Please try another image."
)

# --- Extract Frame ----------------------------------------------------------
FRAME_PROMPT = (
    "🖼 Send me a video and I'll extract a frame from it.\n\n"
    "For the best quality, send it as a File."
)
FRAME_POSITION_PROMPT = "Which frame?"
FRAME_FORMAT_PROMPT = "Which format?"
FRAME_CUSTOM_TIME_PROMPT = (
    "⌨️ Send the time, as seconds or mm:ss.\n\n"
    "For example: <code>12</code> or <code>1:05</code>."
)
FRAME_TIME_REJECTED = (
    "⚠️ I couldn't read that as a time inside this video. Send seconds "
    "(<code>12</code>) or mm:ss (<code>1:05</code>)."
)
FRAME_PROCESSING = "⏳ Extracting the frame..."
FRAME_CHOICE_EXPIRED = "This choice has expired. Please send the video again."

BTN_FRAME_FIRST = "🎬 First Frame"
BTN_FRAME_MIDDLE = "⏱ Middle Frame"
BTN_FRAME_QUARTER = "📍 25%"
BTN_FRAME_THREE_QUARTER = "📍 75%"
BTN_FRAME_CUSTOM = "⌨️ Custom Time"
BTN_FRAME_JPG = "JPG"
BTN_FRAME_PNG = "PNG"
BTN_FRAME_AGAIN = "🖼 Extract Another"

FRAME_UNSUPPORTED = (
    "⚠️ I can't extract a frame from that. Send a video (MP4, MOV, WEBM, MKV…) "
    "— as a video or as a File."
)
FRAME_CORRUPT = (
    "⚠️ I couldn't read this video. It may be damaged. Please try another one."
)
FRAME_TIMEOUT = "⏱ This video took too long to read. Please try a shorter one."
FRAME_NOT_FOUND = (
    "⚠️ There's no frame at that moment. Try another position."
)
FRAME_VERIFY_FAILED = (
    "⚠️ The extracted frame didn't pass my checks, so I didn't send it. "
    "Please try another position."
)


def frame_done(*, seconds: float, width: int, height: int, image_format: str) -> str:
    return (
        "✅ Frame extracted\n\n"
        f"At: {format_clock(seconds)}\n"
        f"Size: {width}×{height}\n"
        f"Format: {image_format.upper()}"
    )

# --- Batch mode -------------------------------------------------------------
BATCH_PROMPT = (
    "📦 Send me multiple photos or videos.\n\n"
    "You can send them one by one or as a Telegram album.\n\n"
    "When you're done, press:\n"
    "✅ Done Uploading"
)
BATCH_CHOOSE_TOOL = "What should I do with these files?"
BATCH_EMPTY = "Send at least one photo or video first."
BATCH_UNSUPPORTED = (
    "⚠️ I skipped that one. A batch takes photos (JPEG, PNG, WEBP) and videos "
    "(MP4, MOV, WEBM, MKV…) — as media or as Files."
)
BATCH_RUNNING = "⏳ Your batch is still running. I'll tell you when it's done."
BATCH_WATERMARK_PROMPT = "Which watermark should I use for every file?"
BATCH_OPTIMIZE_PROMPT = "Which preset should I use for every file?"

BTN_BATCH_DONE = "✅ Done Uploading"
BTN_BATCH_CLEAN = "🧹 Clean Metadata"
BTN_BATCH_WATERMARK = "🖼 Add Watermark"
BTN_BATCH_OPTIMIZE = "🗜 Optimize Media"
BTN_BATCH_NEW_WATERMARK = "✏️ New Watermark"
BTN_BATCH_NEW = "📦 New Batch"


def batch_collected(count: int, maximum: int) -> str:
    """The one collection message, edited as each file arrives."""
    remaining = (
        f"That's the maximum of {maximum}. Press Done Uploading."
        if count >= maximum
        else "Send more or press Done Uploading."
    )
    return (
        "📦 <b>Batch</b>\n"
        f"{count} {'file' if count == 1 else 'files'} received\n\n"
        f"{remaining}"
    )


def batch_full(maximum: int) -> str:
    return f"📦 A batch holds up to {maximum} files. Press Done Uploading."


def batch_progress(done: int, total: int) -> str:
    return f"⏳ <b>Processing batch</b>\n\n{done} / {total} completed"


def batch_summary(
    *, total: int, successful: int, failed: int, skipped: int = 0, cancelled: bool = False
) -> str:
    summary = (
        f"{'📦 Batch stopped' if cancelled else '✅ Batch complete'}\n\n"
        f"Files: {total}\n"
        f"Successful: {successful}\n"
        f"Failed: {failed}"
    )
    if skipped:
        # Honest about the optimizer's "nothing to gain" outcome.
        summary += f"\nAlready efficient (original kept): {skipped}"
    return summary


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
ERROR_DOWNLOAD = (
    "⚠️ Telegram didn't hand the file over in time. "
    "Please send it again."
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
        f"🖼 Watermarks: {stats.watermarks}",
        f"📦 Batches: {stats.batches}",
        f"🧹 Metadata cleans: {stats.metadata_cleans}",
        f"✏️ Metadata changes: {stats.metadata_changes}",
        f"🎞 GIF/MP4 conversions: {stats.conversions}",
        f"🏷 Stickers made: {stats.stickers_made}",
        f"🖼 Frames extracted: {stats.frames}",
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


def stats_system(
    *, version: str, database: str, local_api: bool, active_jobs: int, job_slots: int
) -> str:
    """Admin-only operational footer: what is running, right now."""
    return (
        "\n\n<b>System:</b>\n"
        f"Version: {version}\n"
        f"Database: {database}\n"
        f"Local Bot API: {'on' if local_api else 'off'}\n"
        f"Active media jobs: {active_jobs}/{job_slots}"
    )


RATE_LIMITED = (
    "\u23f3 You're using the tools very quickly. Wait a moment and try again."
)

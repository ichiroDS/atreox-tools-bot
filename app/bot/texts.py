"""Every user-facing string lives here so copy changes never touch handlers."""

from __future__ import annotations

# --- Brand ------------------------------------------------------------------
ATREOX_URL = "https://atreoxai.com"

# --- Menu -------------------------------------------------------------------
BTN_CIRCLE = "\U0001f3a5 Video \u2192 Circle"
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

BTN_CIRCLE_FORWARD_HELP = '🤖 Remove "Forwarded from bot"'
BTN_CIRCLE_AGAIN = "🎥 Make Another Circle"
BTN_GUIDE_IOS = "🍎 iOS"
BTN_GUIDE_ANDROID = "🤖 Android"

# The steps are identical on every platform today. They live in one place so a
# platform guide can diverge (or gain a GIF) without touching the others.
_FORWARD_HELP_STEPS = (
    "1. Press and hold the circle → Forward\n"
    "2. Select the chat/channel\n"
    "3. Open Message Settings / Forwarding Options\n"
    "4. Choose Hide Sender Name\n"
    "5. Send\n\n"
    "The exact wording may differ slightly between iOS, Android and Desktop."
)

CIRCLE_FORWARD_HELP = (
    "🤖 <b>How to hide the bot name when forwarding</b>\n\n"
    + _FORWARD_HELP_STEPS
)
CIRCLE_FORWARD_HELP_IOS = "🍎 <b>iOS</b>\n\n" + _FORWARD_HELP_STEPS
CIRCLE_FORWARD_HELP_ANDROID = (
    "🤖 <b>Android</b>\n\n" + _FORWARD_HELP_STEPS
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
ERROR_TOO_LARGE = "The file is too large for the current bot configuration."
ERROR_GENERIC = "Something went wrong. Let's start over."
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

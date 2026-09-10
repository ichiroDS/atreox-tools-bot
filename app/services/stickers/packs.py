"""The curated list of public Telegram sticker packs, by category.

Only pack *names* live here. Telegram ``file_id`` values are bot-specific and
cannot be written down in advance, so ``scripts/sync_stickers.py`` resolves
them against the live Bot API and writes them to the database.

Adding a pack means adding its name below and re-running the sync.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CuratedPack:
    """A public pack we are happy to surface, and the mood it belongs to."""

    telegram_set_name: str
    category: str
    # Display title. Left empty to adopt whatever Telegram reports, which is
    # what we do for packs whose own title is already good.
    title: str = ""


CURATED_PACKS: tuple[CuratedPack, ...] = (
    # --- reactions ----------------------------------------------------------
    CuratedPack("HotCherry", "reactions", "Hot Cherry"),
    CuratedPack("UtyaDuck", "reactions", "Utya the Duck"),
    CuratedPack("PepeTheFrog", "reactions", "Pepe"),
    CuratedPack("CryingCat", "reactions", "Crying Cat"),
    CuratedPack("SadHamster", "reactions", "Sad Hamster"),
    CuratedPack("Sisyphus", "reactions", "Sisyphus"),
    CuratedPack("AnimatedEmojies", "reactions", "Animated Emoji"),
    # --- cute ---------------------------------------------------------------
    CuratedPack("Persik", "cute", "Persik the Cat"),
    CuratedPack("BlueCat", "cute", "Blue Cat"),
    CuratedPack("CatsPack", "cute", "Must Have Cats"),
    CuratedPack("Cutecats", "cute", "Cute Cats"),
    CuratedPack("CatEmoji", "cute", "Cat Emoji"),
    CuratedPack("Penguin", "cute", "Penguins"),
    CuratedPack("Rabbit", "cute", "Rabbit"),
    CuratedPack("Ruffle", "cute", "Ruffle"),
    # --- flirty -------------------------------------------------------------
    CuratedPack("Valentine", "flirty", "Valentine"),
    CuratedPack("Hearts", "flirty", "Hearts"),
    CuratedPack("Flirt", "flirty", "Bloom"),
    CuratedPack("LoveYou", "flirty", "Love You"),
    CuratedPack("Heart", "flirty", "Heart"),
    # --- aesthetic ----------------------------------------------------------
    CuratedPack("Aesthetic", "aesthetic", "Aesthetic"),
    CuratedPack("Vaporwave", "aesthetic", "Vaporwave"),
    CuratedPack("Whales", "aesthetic", "Whales"),
    CuratedPack("Snoopy", "aesthetic", "Snoopy"),
    CuratedPack("Stars", "aesthetic", "Stars"),
    # --- savage -------------------------------------------------------------
    CuratedPack("Savage", "savage", "Trap Savage"),
    CuratedPack("Devil", "savage", "Diabliyo"),
    CuratedPack("Money", "savage", "Money"),
    CuratedPack("Toffee", "savage", "Toffee"),
    CuratedPack("Shark", "savage", "Shark"),
    # --- morning / night ----------------------------------------------------
    CuratedPack("GoodMorning", "morning_night", "Good Morning"),
    CuratedPack("GoodNight", "morning_night", "Good Night"),
    CuratedPack("Coffee", "morning_night", "Coffee"),
    CuratedPack("Sleep", "morning_night", "Sleep"),
    CuratedPack("Sleepy", "morning_night", "Sleepy"),
    CuratedPack("Dream", "morning_night", "Dream"),
    CuratedPack("Night", "morning_night", "Night"),
    # --- anime --------------------------------------------------------------
    CuratedPack("SouFrierenp_2fx", "anime", "Sousou no Frieren"),
    CuratedPack("Randomharkhd", "anime", "Anime Daily"),
    CuratedPack("longanimepack", "anime", "Long Girls"),
    CuratedPack("OurOmegaLeadernim", "anime", "Our Omega Leader"),
)


def packs_for_category(category: str) -> tuple[CuratedPack, ...]:
    return tuple(p for p in CURATED_PACKS if p.category == category)


def validate_packs(packs: tuple[CuratedPack, ...] = CURATED_PACKS) -> None:
    """Fail fast on a malformed catalog (called at import time and in tests)."""
    from app.services.stickers.catalog import CATEGORY_KEYS

    seen: set[str] = set()
    for pack in packs:
        if not pack.telegram_set_name:
            raise ValueError("every curated pack needs a telegram_set_name")
        if pack.telegram_set_name in seen:
            raise ValueError(f"duplicate curated pack: {pack.telegram_set_name}")
        seen.add(pack.telegram_set_name)
        if pack.category not in CATEGORY_KEYS:
            raise ValueError(
                f"{pack.telegram_set_name}: unknown category {pack.category!r}"
            )


validate_packs()

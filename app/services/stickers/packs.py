"""The curated list of public Telegram sticker packs, by category.

Only pack *names* live here. Telegram ``file_id`` values are bot-specific and
cannot be written down in advance, so ``scripts/sync_stickers.py`` resolves
them against the live Bot API and writes them to the database.

Adding a pack means adding its name below and re-running the sync.

Titles are given explicitly rather than adopted from Telegram: a lot of good
packs carry a promo line as their title ("Больше стикеров тут: @...", "@fixfox
@sharkhd"), which is someone else's advertising, not a category label.
"""

from __future__ import annotations

from dataclasses import dataclass

# Which sticker of a pack represents it. Packs open with their most generic
# faces, so a fixed position a little way in shows something with character -
# and, being fixed, it shows the *same* sticker every time, which is what
# makes a category recognisable instead of a lottery. 1-based.
DEFAULT_SAMPLE_POSITION = 5


@dataclass(frozen=True)
class CuratedPack:
    """A public pack we are happy to surface, and the mood it belongs to."""

    telegram_set_name: str
    category: str
    # Display title. Left empty to adopt whatever Telegram reports, which is
    # what we do for packs whose own title is already good.
    title: str = ""
    # Which sticker stands for this pack, 1-based; see the constant above.
    sample_position: int = DEFAULT_SAMPLE_POSITION


CURATED_PACKS: tuple[CuratedPack, ...] = (
    # --- reactions ----------------------------------------------------------
    CuratedPack("HotCherry", "reactions", "Hot Cherry"),
    CuratedPack("UtyaDuck", "reactions", "Utya the Duck"),
    CuratedPack("PepeTheFrog", "reactions", "Pepe"),
    CuratedPack("CryingCat", "reactions", "Crying Cat"),
    CuratedPack("SadHamster", "reactions", "Sad Hamster"),
    CuratedPack("DisgruntledToad", "reactions", "Disgruntled Toad", sample_position=6),
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
    CuratedPack("cybercats_stickers", "cute", "Cyber Cats"),
    CuratedPack("nzrfzsepnp311df9_by_Stickerevobot", "cute", "Hearts & Love"),
    # --- anime --------------------------------------------------------------
    CuratedPack("SouFrierenp_2fx", "anime", "Sousou no Frieren"),
    CuratedPack("Randomharkhd", "anime", "Anime Daily"),
    CuratedPack("BanG_Dream_Ave_Mujica_P3", "anime", "BanG Dream! Ave Mujica"),
    CuratedPack("Vermeil_Part_1_by_Fix_x_Fox", "anime", "Vermeil"),
    CuratedPack("wtffffffffffDD", "anime", "Daily Usable I"),
    CuratedPack("wtfffffff_2_Fix_x_Fox", "anime", "Daily Usable II"),
    CuratedPack("devradio", "anime", "Dev Radio"),
    CuratedPack("Marin_Kitagawa_p1", "anime", "Marin Kitagawa"),
    CuratedPack("marinkitagawaanime", "anime", "Marin Kitagawa II"),
    CuratedPack("Adopotet", "anime", "Ado"),
)


def packs_for_category(category: str) -> tuple[CuratedPack, ...]:
    return tuple(p for p in CURATED_PACKS if p.category == category)


def sample_position_for(
    set_name: str, packs: tuple[CuratedPack, ...] = CURATED_PACKS
) -> int:
    """Which sticker represents this pack, 1-based.

    A pack the catalog no longer lists (a row left in the database from an
    older sync) falls back to the default rather than failing.
    """
    for pack in packs:
        if pack.telegram_set_name == set_name:
            return pack.sample_position
    return DEFAULT_SAMPLE_POSITION


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
        if pack.sample_position < 1:
            raise ValueError(
                f"{pack.telegram_set_name}: sample_position is 1-based"
            )


validate_packs()

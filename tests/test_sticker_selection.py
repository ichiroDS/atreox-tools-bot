from __future__ import annotations

import random

import pytest

from app.services.stickers import select_distinct_stickers
from app.services.stickers.service import StickerFinderService
from tests.conftest import make_sample, make_set


def catalog(n_sets: int, samples_per_set: int = 3):
    return [
        make_set(
            f"pack_{i}",
            [make_sample(f"file_{i}_{j}") for j in range(samples_per_set)],
        )
        for i in range(n_sets)
    ]


@pytest.mark.parametrize("seed", range(25))
def test_never_returns_two_stickers_from_the_same_pack(seed):
    picks = select_distinct_stickers(catalog(12), 6, rng=random.Random(seed))
    names = [p.set_name for p in picks]
    assert len(names) == 6
    assert len(set(names)) == 6


def test_returns_at_most_the_requested_count():
    picks = select_distinct_stickers(catalog(20), 6, rng=random.Random(1))
    assert len(picks) == 6


def test_returns_fewer_when_catalog_is_small():
    picks = select_distinct_stickers(catalog(3), 6, rng=random.Random(1))
    assert len(picks) == 3
    assert len({p.set_name for p in picks}) == 3


def test_skips_sets_without_usable_samples():
    sets = [
        make_set("empty", []),
        make_set("disabled", [make_sample("x", enabled=False)]),
        make_set("good", [make_sample("ok")]),
    ]
    picks = select_distinct_stickers(sets, 6, rng=random.Random(0))
    assert [p.set_name for p in picks] == ["good"]
    assert picks[0].file_id == "ok"


def test_excluded_packs_are_avoided_when_alternatives_exist():
    picks = select_distinct_stickers(
        catalog(10), 4, rng=random.Random(3), exclude_set_names={"pack_0", "pack_1"}
    )
    names = {p.set_name for p in picks}
    assert not names & {"pack_0", "pack_1"}


def test_exclusion_falls_back_rather_than_returning_nothing():
    sets = catalog(2)
    picks = select_distinct_stickers(
        sets, 4, rng=random.Random(3), exclude_set_names={"pack_0", "pack_1"}
    )
    assert len(picks) == 2


def test_empty_catalog_returns_empty_list():
    assert select_distinct_stickers([], 6, rng=random.Random(0)) == []


class FakeRepository:
    def __init__(self, sets):
        self._sets = sets
        self.calls: list[str] = []

    async def enabled_sets_for_category(self, category: str):
        self.calls.append(category)
        return self._sets


async def test_service_queries_the_requested_category():
    repository = FakeRepository(catalog(8))
    service = StickerFinderService(repository, rng=random.Random(7))
    picks = await service.find("cute", count=6)
    assert repository.calls == ["cute"]
    assert len({p.set_name for p in picks}) == 6


# --- the sticker that stands for a pack is fixed, not drawn -------------------


def _pack(name: str, size: int):
    return make_set(name, [make_sample(f"{name}-{position}") for position in range(1, size + 1)])


@pytest.mark.parametrize("seed", range(10))
def test_a_pack_always_shows_the_same_sticker(seed):
    """A category is recognisable only if a pack looks the same every time."""
    picks = select_distinct_stickers(
        [_pack("pack_a", 8), _pack("pack_b", 8)], 2, rng=random.Random(seed),
        position_for=lambda _name: 5,
    )
    assert {p.file_id for p in picks} == {"pack_a-5", "pack_b-5"}


def test_the_default_position_is_the_fifth_sticker():
    from app.services.stickers.packs import DEFAULT_SAMPLE_POSITION, sample_position_for

    assert DEFAULT_SAMPLE_POSITION == 5
    assert sample_position_for("HotCherry") == 5
    # A pack with a position of its own keeps it.
    assert sample_position_for("DisgruntledToad") == 6
    # A row left behind by an older sync still resolves.
    assert sample_position_for("gone_from_the_catalog") == 5


def test_a_short_pack_falls_back_to_its_last_sticker():
    picks = select_distinct_stickers(
        [_pack("tiny", 2)], 1, rng=random.Random(0), position_for=lambda _name: 5
    )
    assert [p.file_id for p in picks] == ["tiny-2"]


def test_the_position_is_read_from_the_curated_catalog_by_default():
    """No explicit position_for: the catalog's own numbers are used."""
    picks = select_distinct_stickers([_pack("DisgruntledToad", 8)], 1, rng=random.Random(0))
    assert [p.file_id for p in picks] == ["DisgruntledToad-6"]


def test_disabled_stickers_do_not_shift_the_position():
    """Position counts the stickers we can actually send."""
    samples = [make_sample(f"p-{i}") for i in range(1, 9)]
    samples[1] = make_sample("p-2", enabled=False)
    picks = select_distinct_stickers(
        [make_set("p", samples)], 1, rng=random.Random(0), position_for=lambda _name: 5
    )
    # The second sticker is unusable, so the fifth usable one is the sixth.
    assert [p.file_id for p in picks] == ["p-6"]

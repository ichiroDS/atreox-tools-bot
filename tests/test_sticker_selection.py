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

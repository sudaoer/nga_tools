from __future__ import annotations

import pytest

from nga_tools.image_cluster.lsh import (
    CandidatePair,
    LshConfig,
    collect_distance_histogram,
    generate_candidate_pairs,
)


def test_empty_input_returns_empty() -> None:
    assert generate_candidate_pairs({}, LshConfig()) == []
    assert generate_candidate_pairs({"a": "0000000000000000"}, LshConfig()) == []


def test_identical_hashes_become_candidates() -> None:
    hashes = {
        "a.png": "ffff0000ffff0000",
        "b.png": "ffff0000ffff0000",
        "c.png": "0000ffff0000ffff",
    }
    pairs = generate_candidate_pairs(hashes, LshConfig(bands=4))

    pair_keys = {(p.a, p.b) for p in pairs}
    assert ("a.png", "b.png") in pair_keys
    assert ("a.png", "c.png") not in pair_keys
    assert ("b.png", "c.png") not in pair_keys


def test_pairs_deduplicated_across_bands() -> None:
    hashes = {
        "a.png": "0000000000000000",
        "b.png": "0000000000000000",
    }
    pairs = generate_candidate_pairs(hashes, LshConfig(bands=4))
    assert pairs == [CandidatePair(a="a.png", b="b.png")]


def test_single_band_difference_still_candidate() -> None:
    hashes = {
        "a.png": "ffff0000ffff0000",
        "b.png": "ffff0000ffff0001",
    }
    pairs = generate_candidate_pairs(hashes, LshConfig(bands=4))
    assert len(pairs) == 1
    assert pairs[0] == CandidatePair(a="a.png", b="b.png")


def test_completely_different_not_candidates() -> None:
    hashes = {
        "a.png": "ffffffffffffffff",
        "b.png": "0000000000000000",
    }
    pairs = generate_candidate_pairs(hashes, LshConfig(bands=4))
    assert pairs == []


def test_invalid_bands_raises() -> None:
    hashes = {"a.png": "0000000000000000", "b.png": "0000000000000000"}
    with pytest.raises(ValueError):
        generate_candidate_pairs(hashes, LshConfig(bands=3))
    with pytest.raises(ValueError):
        generate_candidate_pairs(hashes, LshConfig(bands=0))


def test_candidate_pair_sorted_order() -> None:
    p = CandidatePair.sorted("z.png", "a.png")
    assert p.a == "a.png"
    assert p.b == "z.png"


def test_distance_histogram() -> None:
    hashes = {
        "a.png": "0000000000000000",
        "b.png": "0000000000000000",
        "c.png": "0000000000000001",
        "d.png": "ffffffffffffffff",
    }
    pairs = [
        CandidatePair(a="a.png", b="b.png"),
        CandidatePair(a="a.png", b="c.png"),
        CandidatePair(a="a.png", b="d.png"),
    ]
    histogram = collect_distance_histogram(pairs, hashes)

    assert histogram[0] == 1
    assert histogram[1] == 1
    assert histogram[64] == 1
    assert sum(histogram) == 3


def test_histogram_skips_missing_hashes() -> None:
    hashes = {"a.png": "0000000000000000"}
    pairs = [CandidatePair(a="a.png", b="missing.png")]
    histogram = collect_distance_histogram(pairs, hashes)
    assert sum(histogram) == 0


def test_pairs_are_sorted_and_deterministic() -> None:
    hashes = {
        f"img{i}.png": "1234567890abcdef"
        for i in range(5)
    }
    pairs1 = generate_candidate_pairs(hashes, LshConfig(bands=4))
    pairs2 = generate_candidate_pairs(hashes, LshConfig(bands=4))
    assert pairs1 == pairs2
    assert pairs1 == sorted(pairs1, key=lambda p: (p.a, p.b))

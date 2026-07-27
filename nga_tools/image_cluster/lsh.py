from __future__ import annotations

from dataclasses import dataclass

_HASH_BITS = 64


@dataclass(frozen=True)
class LshConfig:
    bands: int = 4
    seed: int = 20260717


@dataclass(frozen=True)
class CandidatePair:
    a: str
    b: str


def _validate_bands(bands: int) -> int:
    if bands <= 0:
        raise ValueError(f"lsh_bands 必须为正数：{bands}")
    if _HASH_BITS % bands != 0:
        raise ValueError(
            f"lsh_bands={bands} 必须能整除 {_HASH_BITS}"
        )
    return bands


def generate_candidate_pairs(
    hashes: dict[str, str],
    config: LshConfig,
) -> list[CandidatePair]:
    if len(hashes) < 2:
        return []

    bands = _validate_bands(config.bands)
    bits_per_band = _HASH_BITS // bands
    mask = (1 << bits_per_band) - 1

    seen: set[tuple[str, str]] = set()
    for band_idx in range(bands):
        shift = band_idx * bits_per_band
        buckets: dict[int, list[str]] = {}
        for path, hash_hex in hashes.items():
            value = int(hash_hex, 16)
            band_value = (value >> shift) & mask
            buckets.setdefault(band_value, []).append(path)

        for members in buckets.values():
            if len(members) < 2:
                continue
            count = len(members)
            for i in range(count):
                for j in range(i + 1, count):
                    seen.add(
                        (min(members[i], members[j]), max(members[i], members[j]))
                    )

    return sorted(
        (CandidatePair(a=a, b=b) for a, b in seen),
        key=lambda p: (p.a, p.b),
    )

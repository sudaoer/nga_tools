from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

from nga_tools.core.hashing import hash_object

POST_VERSION_SELECTIONS_FINGERPRINT_VERSION = 1


class PostVersionSelection(TypedDict):
    version_id: int
    source_hash: str
    selected_at: str


def post_version_selections_fingerprint(
    selections: Mapping[int, PostVersionSelection],
) -> str:
    return hash_object(
        {
            "version": POST_VERSION_SELECTIONS_FINGERPRINT_VERSION,
            "selections": {
                str(lou): {
                    "version_id": selections[lou]["version_id"],
                    "source_hash": selections[lou]["source_hash"],
                }
                for lou in sorted(selections)
            },
        }
    )

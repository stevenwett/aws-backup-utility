"""S3 storage classes supported by ``aws s3 sync --storage-class``.

The order here is the order shown in the interactive tier picker, roughly from
hottest/most-expensive to coldest/cheapest.
"""

from typing import Dict, List, NamedTuple


class Tier(NamedTuple):
    name: str
    description: str


TIERS: List[Tier] = [
    Tier("STANDARD", "Frequent access, lowest latency"),
    Tier("STANDARD_IA", "Infrequent access, millisecond retrieval"),
    Tier("ONEZONE_IA", "Infrequent access, single AZ (cheaper, less durable)"),
    Tier("INTELLIGENT_TIERING", "Auto-tiers between access classes by usage"),
    Tier("GLACIER_IR", "Archive with instant retrieval"),
    Tier("GLACIER", "Archive, retrieval in minutes to hours"),
    Tier("DEEP_ARCHIVE", "Cheapest archive, retrieval in ~12 hours"),
]

TIER_NAMES: List[str] = [t.name for t in TIERS]

_DESCRIPTIONS: Dict[str, str] = {t.name: t.description for t in TIERS}

DEFAULT_TIER = "STANDARD_IA"


def is_valid(name: str) -> bool:
    return name in _DESCRIPTIONS


def describe(name: str) -> str:
    return _DESCRIPTIONS.get(name, "")

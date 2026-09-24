"""Small ACL boundary used before evidence reaches the answer builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: str
    groups: frozenset[str] = frozenset()


def can_read(row: dict[str, Any], principal: Principal) -> bool:
    allowed = row.get("allowed_groups")
    if not allowed:
        return True
    return bool(set(allowed) & principal.groups)

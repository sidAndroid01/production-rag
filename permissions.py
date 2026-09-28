"""Principal and document ACL rules, applied before evidence reaches generation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling, as established by the server from their credential."""

    tenant_id: str
    user_id: str
    groups: frozenset[str] = frozenset()


def normalize_groups(value: Any) -> tuple[str, ...]:
    """Validate a client-supplied ACL: a list of non-empty group names."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(group, str) and group.strip() for group in value
    ):
        raise ValueError("allowed_groups must be a list of non-empty strings")
    return tuple(sorted({group.strip() for group in value}))


def can_read_groups(allowed: Iterable[str] | None, principal: Principal) -> bool:
    """An empty ACL is tenant-wide; otherwise the caller needs one shared group."""
    allowed_set = set(allowed or ())
    return not allowed_set or bool(allowed_set & principal.groups)


def can_read(row: dict[str, Any], principal: Principal) -> bool:
    return row.get("tenant_id") == principal.tenant_id and can_read_groups(
        row.get("allowed_groups"), principal
    )

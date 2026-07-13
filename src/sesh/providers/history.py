"""Pure helpers for projecting append-only provider histories."""

from __future__ import annotations


def active_ancestor_ids(
    parents: dict[str, str | None], leaf_id: str | None
) -> set[str] | None:
    """Return the reachable ancestry ending at *leaf_id*.

    ``None`` means the lineage is unusable and callers should preserve their
    legacy linear behavior. A missing parent terminates a valid suffix; cycles
    and unknown leaves are treated as ambiguous rather than hiding records.
    """
    if not leaf_id or leaf_id not in parents:
        return None

    active: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in active:
            return None
        if current not in parents:
            break
        active.add(current)
        current = parents[current]
    return active

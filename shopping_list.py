"""Shared shopping list with fuzzy dedup for the household MCP assistant."""

import difflib

from common import ShoppingItem


def fuzzy_dedup(items: list[ShoppingItem], new_name: str, cutoff: float = 0.6) -> ShoppingItem | None:
    """Find a similar unbought item by name using fuzzy matching.

    Returns the matched ShoppingItem or None if no close match found.
    """
    new_norm = new_name.strip().lower()
    # Build a map of existing unbought item names (normalized) -> item
    existing = {}
    for item in items:
        if item.bought:
            continue
        existing[item.name.strip().lower()] = item

    if not existing:
        return None

    # exact match first
    if new_norm in existing:
        return existing[new_norm]

    # fuzzy match
    matches = difflib.get_close_matches(new_norm, list(existing.keys()), n=1, cutoff=cutoff)
    if matches:
        return existing[matches[0]]

    return None

"""Shared shopping list with fuzzy dedup for the household MCP assistant."""

import difflib
import json
import sys
import time
from dataclasses import dataclass, field, asdict

SHOPPING_FILE = "shopping_list.json"


@dataclass
class ShoppingItem:
    name: str
    added_by: str
    quantity: str | None = None
    category: str | None = None
    bought: bool = False
    created_at: float = field(default_factory=time.time)


def load_shopping_list() -> list[ShoppingItem]:
    try:
        with open(SHOPPING_FILE) as f:
            data = json.load(f)
            return [ShoppingItem(**c) for c in data]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_shopping_list(items: list[ShoppingItem]) -> None:
    with open(SHOPPING_FILE, "w") as f:
        json.dump([asdict(c) for c in items], f, indent=2)


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

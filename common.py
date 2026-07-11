"""Shared models, utilities, and app state for the household MCP assistant."""

import difflib
import json
import sys
import time
from dataclasses import dataclass, field, asdict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PERSON_LABELS = {"person_1": "Divyanshu Garg", "person_2": "dvinix"}
COMMITMENTS_FILE = "commitments.json"
SHOPPING_FILE = "shopping_list.json"


# ---------------------------------------------------------------------------
# Safe print for Windows terminals
# ---------------------------------------------------------------------------

def safe_print(*args, **kwargs):
    enc = sys.stdout.encoding or "utf-8"
    text = " ".join(str(a) for a in args)
    kwargs.pop("file", None)
    print(text.encode(enc, errors="replace").decode(enc), **kwargs)


# ---------------------------------------------------------------------------
# Commitment data model + persistence
# ---------------------------------------------------------------------------

@dataclass
class Commitment:
    person: str
    task: str
    source_text: str
    deadline: str | None
    status: str  # "open" | "done"
    created_at: float = field(default_factory=time.time)


def load_commitments() -> list[Commitment]:
    try:
        with open(COMMITMENTS_FILE) as f:
            data = json.load(f)
            return [Commitment(**c) for c in data]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_commitments(commitments: list[Commitment]) -> None:
    with open(COMMITMENTS_FILE, "w") as f:
        json.dump([asdict(c) for c in commitments], f, indent=2)


# ---------------------------------------------------------------------------
# Person ID fuzzy matching
# ---------------------------------------------------------------------------

def fuzzy_person_id(raw: str) -> str | None:
    """Try fuzzy match of a raw name to a known person key."""
    raw_norm = raw.lower().replace(" ", "").replace("-", "")
    known = {pid: label.lower().replace(" ", "").replace("-", "") for pid, label in PERSON_LABELS.items()}
    # exact match
    for pid, norm in known.items():
        if raw_norm == norm:
            return pid
    # substring match
    for pid, norm in known.items():
        if raw_norm in norm or norm in raw_norm:
            return pid
    # fuzzy (levenshtein-ish) match
    matches = difflib.get_close_matches(raw_norm, list(known.values()), n=1, cutoff=0.6)
    if matches:
        for pid, norm in known.items():
            if norm == matches[0]:
                return pid
    return None


# ---------------------------------------------------------------------------
# App state — bundles runtime data passed around the application
# ---------------------------------------------------------------------------

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

@dataclass
class AppState:
    commitments: list[Commitment]
    shopping_items: list[ShoppingItem]
    active_person: str = "person_1"
    active_label: str = "Divyanshu Garg"

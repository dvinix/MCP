"""Standalone nudge generator for household commitments.

Reads commitments.json, parses free-text deadlines, and outputs
a grouped urgency report (overdue / due today / due soon).

Can be run standalone or imported by host.py.
"""

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------------
# Shared data model (mirrored from host.py for standalone safety)
# ---------------------------------------------------------------------------

COMMITMENTS_FILE = "commitments.json"
PERSON_LABELS = {"person_1": "Divyanshu Garg", "person_2": "dvinix"}


@dataclass
class Commitment:
    person: str
    task: str
    source_text: str
    deadline: str | None
    status: str
    created_at: float = 0.0


def load_commitments() -> list[Commitment]:
    try:
        with open(COMMITMENTS_FILE) as f:
            data = json.load(f)
            return [Commitment(**c) for c in data]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def safe_print(*args, **kwargs):
    enc = sys.stdout.encoding or "utf-8"
    text = " ".join(str(a) for a in args)
    kwargs.pop("file", None)
    print(text.encode(enc, errors="replace").decode(enc), **kwargs)


# ---------------------------------------------------------------------------
# Deadline parser
# ---------------------------------------------------------------------------

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _next_weekday(target: int, today: date) -> date:
    """Return next occurrence of target weekday (0=Mon), or today if today IS that day."""
    days_ahead = (target - today.weekday() + 7) % 7
    return today if days_ahead == 0 else today + timedelta(days=days_ahead)


def parse_deadline(deadline_str: str | None, today: date | None = None) -> date | None:
    """Parse a free-text deadline string into an approximate date, or None."""
    if not deadline_str or not deadline_str.strip():
        return None
    today = today or date.today()
    s = deadline_str.strip().lower()

    # "by {dayname}" -> strip "by " and recurse
    if s.startswith("by "):
        s = s[3:].strip()

    # Day names
    if s in WEEKDAYS:
        return _next_weekday(WEEKDAYS[s], today)

    # Relative keywords
    if s in ("tomorrow",):
        return today + timedelta(days=1)
    if s in ("today", "eod"):
        return today
    if s == "this weekend":
        return _next_weekday(5, today)  # Saturday
    if s == "this week":
        return _next_weekday(6, today)  # Sunday

    return None


# ---------------------------------------------------------------------------
# Nudge text generation
# ---------------------------------------------------------------------------

def _category(deadline_date: date, today: date) -> str:
    if deadline_date < today:
        return "OVERDUE"
    if deadline_date == today:
        return "DUE_TODAY"
    if deadline_date <= today + timedelta(days=7):
        return "DUE_SOON"
    return ""


def generate_nudge_text(commitments: list, tz_name: str | None = None) -> str:
    """Build a grouped nudge report from a list of Commitment-like objects."""
    open_items = [c for c in commitments if getattr(c, "status", "") == "open"]
    if not open_items:
        return ""

    # Determine today in the configured timezone
    if tz_name:
        try:
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo(tz_name)).date()
        except Exception:
            today = date.today()
    else:
        today = date.today()

    # Build per-person entries with category
    person_entries: dict[str, list[tuple[str, str, str | None]]] = {}
    for c in open_items:
        pid = getattr(c, "person", "unknown")
        label = PERSON_LABELS.get(pid, pid)
        dl = getattr(c, "deadline", None)
        task = getattr(c, "task", "")
        parsed = parse_deadline(dl, today)
        cat = _category(parsed, today) if parsed else ""
        person_entries.setdefault(label, []).append((cat, task, dl))

    # Sort within each person: OVERDUE(0) < DUE_TODAY(1) < DUE_SOON(2) < ""(3)
    rank = {"OVERDUE": 0, "DUE_TODAY": 1, "DUE_SOON": 2}
    lines = ["=== NUDGES ==="]
    for person_label in sorted(person_entries.keys()):
        entries = person_entries[person_label]
        entries.sort(key=lambda e: rank.get(e[0], 3))
        lines.append(
            f"\n[{person_label}] — {len(entries)} item{'s' if len(entries) > 1 else ''}"
        )
        for cat, task, dl in entries:
            if cat == "OVERDUE":
                lines.append(f"  ⏰ OVERDUE: {task} (deadline was {dl})")
            elif cat == "DUE_TODAY":
                lines.append(f"  \U0001f4c5 DUE TODAY: {task} (deadline: {dl})")
            elif cat == "DUE_SOON":
                lines.append(f"  \U0001f4c5 DUE SOON: {task} (deadline: {dl})")
            else:
                lines.append(f"  {task}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main():
    commitments = load_commitments()
    text = generate_nudge_text(commitments)
    if text.strip():
        safe_print()
        safe_print(text)
        safe_print()
    else:
        safe_print("\nNo upcoming deadlines.\n")


if __name__ == "__main__":
    main()

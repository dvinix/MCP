import asyncio
import difflib
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, asdict

from dotenv import load_dotenv
from groq import AsyncGroq
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from check_commitments import generate_nudge_text
from shopping_list import ShoppingItem, load_shopping_list, save_shopping_list, fuzzy_dedup

load_dotenv()

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

COMMITMENTS_FILE = "commitments.json"

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
# Config
# ---------------------------------------------------------------------------

def load_config(path="config.json") -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Local tool definitions (not MCP — handled directly in host.py)
# ---------------------------------------------------------------------------

LOCAL_SENTINEL = "__local__"

LOCAL_TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "record_commitment",
            "description": "Save a commitment, task, or promise someone made. Call this when someone says they'll handle something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "What needs to be done"},
                    "person": {"type": "string", "description": "Who is responsible (omit to use the active speaker)"},
                    "deadline_guess": {"type": "string", "description": "When it needs to be done (e.g. 'this week', 'Thursday', 'by tomorrow', or leave blank)"},
                    "source_text": {"type": "string", "description": "The original sentence that expressed the commitment"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_open_commitments",
            "description": "Show open commitments, optionally filtered by person.",
            "parameters": {
                "type": "object",
                "properties": {
                    "person": {"type": "string", "description": "Filter by person name (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nudges",
            "description": "Show overdue and upcoming deadlines for open commitments, grouped by person.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_shopping_item",
            "description": "Add an item to the shared shopping list. If a similar item already exists, it will be noted rather than duplicated.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Item name (e.g. 'milk', 'paper towels')"},
                    "quantity": {"type": "string", "description": "Optional quantity (e.g. '2 liters', 'a bunch')"},
                    "category": {"type": "string", "description": "Optional category (e.g. 'dairy', 'produce', 'household')"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_shopping_list",
            "description": "Show the shared shopping list, optionally filtered by category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "description": "Filter by category (optional)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_bought",
            "description": "Mark a shopping list item as bought, by name or 1-based index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Item name to mark as bought"},
                    "index": {"type": "integer", "description": "1-based index from list_shopping_list"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_bought",
            "description": "Remove all bought items from the shopping list.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


def _fuzzy_person_id(raw: str) -> str | None:
    """Try fuzzy match of a raw name to a known person key."""
    import difflib
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

def handle_local_tool(name: str, args: dict, commitments: list[Commitment], shopping_items: list[ShoppingItem], active_person: str, active_label: str) -> str:
    """Execute a local tool and return text result."""
    if name == "record_commitment":
        person_arg = args.get("person", active_label)
        # Map back to person key using fuzzy matching; fall back to active speaker
        mapped = _fuzzy_person_id(person_arg)
        person_arg = mapped if mapped else active_person
        commitment = Commitment(
            person=person_arg,
            task=args.get("task", ""),
            source_text=args.get("source_text", ""),
            deadline=args.get("deadline_guess"),
            status="open",
        )
        commitments.append(commitment)
        save_commitments(commitments)
        return f"Committed: {commitment.task} (by {person_arg})" + (f" | deadline: {commitment.deadline}" if commitment.deadline else "")

    if name == "list_open_commitments":
        person_filter = args.get("person")
        results = []
        for c in commitments:
            if c.status != "open":
                continue
            if person_filter:
                label = PERSON_LABELS.get(c.person, c.person)
                if person_filter.lower() not in (c.person.lower(), label.lower()):
                    continue
            label = PERSON_LABELS.get(c.person, c.person)
            line = f"- {c.task} (by {label})"
            if c.deadline:
                line += f" | deadline: {c.deadline}"
            results.append(line)
        if not results:
            return "No open commitments found."
        return "\n".join(results)

    if name == "get_nudges":
        return generate_nudge_text(commitments)

    # --- shopping list ---
    if name == "add_shopping_item":
        item_name = args.get("name", "").strip()
        if not item_name:
            return "Error: item name is required."
        match = fuzzy_dedup(shopping_items, item_name)
        if match:
            label = PERSON_LABELS.get(match.added_by, match.added_by)
            return f"Similar item '{match.name}' already on the list (added by {label}). Consider marking it bought instead."
        item = ShoppingItem(
            name=item_name,
            added_by=active_person,
            quantity=args.get("quantity"),
            category=args.get("category"),
        )
        shopping_items.append(item)
        save_shopping_list(shopping_items)
        parts = [f"Added '{item_name}' to shopping list."]
        if item.quantity:
            parts.append(f" ({item.quantity})")
        if item.category:
            parts.append(f" [{item.category}]")
        return " ".join(parts)

    if name == "list_shopping_list":
        category_filter = args.get("category")
        unbought = [i for i in shopping_items if not i.bought and (not category_filter or i.category == category_filter)]
        bought = [i for i in shopping_items if i.bought and (not category_filter or i.category == category_filter)]
        if not shopping_items:
            return "Shopping list is empty."
        lines = []
        if unbought:
            lines.append("Still needed:")
            for idx, item in enumerate(unbought, 1):
                label = PERSON_LABELS.get(item.added_by, item.added_by)
                parts = [f"  {idx}. {item.name}"]
                if item.quantity:
                    parts.append(f"({item.quantity})")
                parts.append(f"[by {label}]")
                if item.category:
                    parts.append(f"[{item.category}]")
                lines.append(" ".join(parts))
        if bought:
            lines.append("\nBought:")
            for idx, item in enumerate(bought, 1):
                label = PERSON_LABELS.get(item.added_by, item.added_by)
                parts = [f"  {idx}. {item.name}"]
                if item.quantity:
                    parts.append(f"({item.quantity})")
                parts.append(f"[by {label}]")
                if item.category:
                    parts.append(f"[{item.category}]")
                lines.append(" ".join(parts))
        return "\n".join(lines)

    if name == "mark_bought":
        item_name = args.get("name")
        item_index = args.get("index")
        if item_index is not None:
            idx = int(item_index) - 1
            unbought = [i for i in shopping_items if not i.bought]
            if 0 <= idx < len(unbought):
                unbought[idx].bought = True
                save_shopping_list(shopping_items)
                return f"Marked '{unbought[idx].name}' as bought."
            return f"Error: index {item_index} out of range."
        if item_name:
            names = {i.name.strip().lower(): i for i in shopping_items if not i.bought}
            matches = difflib.get_close_matches(item_name.strip().lower(), list(names.keys()), n=1, cutoff=0.6)
            if matches:
                item = names[matches[0]]
                item.bought = True
                save_shopping_list(shopping_items)
                return f"Marked '{item.name}' as bought."
            return f"Error: no unbought item matching '{item_name}'."
        return "Error: provide either name or index."

    if name == "clear_bought":
        before = sum(1 for i in shopping_items if i.bought)
        shopping_items[:] = [i for i in shopping_items if not i.bought]
        save_shopping_list(shopping_items)
        return f"Removed {before} bought item(s) from the shopping list."

    return f"Error: unknown local tool '{name}'"


# ---------------------------------------------------------------------------
# Groq retry helper
# ---------------------------------------------------------------------------

async def groq_chat_with_retry(client, max_retries=5, base_delay=2.0, **kwargs):
    last_exc = None
    for attempt in range(max_retries):
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as exc:
            last_exc = exc
            status = getattr(exc, "status_code", None) or (
                getattr(exc, "response", None) and exc.response.status_code
            )
            if status == 429 or "rate limit" in str(exc).lower():
                delay = base_delay * (2**attempt)
                safe_print(f"  [Rate limited - retrying in {delay:.0f}s (attempt {attempt+1}/{max_retries})]")
                await asyncio.sleep(delay)
                continue
            raise
    raise last_exc


# ---------------------------------------------------------------------------
# Tool namespace helpers
# ---------------------------------------------------------------------------

NS_DELIM = "__"

def namespaced_name(server_name: str, tool_name: str) -> str:
    return f"{server_name}{NS_DELIM}{tool_name}"


# ---------------------------------------------------------------------------
# Tool schema conversion  (MCP -> OpenAI-compatible)
# ---------------------------------------------------------------------------

def mcp_tool_to_openai(server_name: str, tool, owner: str):
    ns = namespaced_name(server_name, tool.name)
    return {
        "type": "function",
        "function": {
            "name": ns,
            "description": f"[owner:{owner}] {tool.description or ''}",
            "parameters": tool.inputSchema,
        },
    }


# ---------------------------------------------------------------------------
# Confirmation gate for write operations
# ---------------------------------------------------------------------------

WRITE_PREFIXES = ("create_", "delete_", "update_", "edit_", "insert_", "write_", "move_")

def is_write_tool(tool_name: str) -> bool:
    return tool_name.lower().startswith(WRITE_PREFIXES)


# ---------------------------------------------------------------------------
# Person/identity helpers
# ---------------------------------------------------------------------------

PERSON_LABELS = {"person_1": "Divyanshu Garg", "person_2": "dvinix"}

def select_person() -> str:
    safe_print("\nWho is speaking?")
    safe_print("1-Divyanshu Garg")
    safe_print("2-dvinix")
    choice = input("Choice (1/2): ").strip()
    person = "person_2" if choice == "2" else "person_1"
    safe_print()
    return person


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    config = load_config()

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        safe_print("Error: GROQ_API_KEY not set in .env file")
        sys.exit(1)

    groq = AsyncGroq(api_key=api_key)

    # Build owner lookup: server_name -> owner
    server_owners: dict[str, str] = {}
    for name, cfg in config["servers"].items():
        server_owners[name] = cfg.get("owner", "unknown")

    # Calendar ID per person (single Google account, multiple calendars)
    calendar_map: dict[str, str] = config.get("calendar_map", {})
    timezone: str = config.get("timezone", "UTC")

    # Load persisted commitments
    commitments: list[Commitment] = load_commitments()

    # Load shared shopping list
    shopping_items: list[ShoppingItem] = load_shopping_list()

    # --- person selection ---
    active_person = select_person()
    active_label = PERSON_LABELS.get(active_person, active_person)

    # --- start servers & collect tools ---
    sessions: dict[str, ClientSession] = {}
    tool_owner: dict[str, str] = {}       # namespaced -> server name (or LOCAL_SENTINEL)
    tool_original: dict[str, str] = {}     # namespaced -> original tool name
    groq_tools = []
    server_count = 0

    async with AsyncExitStack() as stack:
        for name, cfg in config["servers"].items():
            owner = server_owners.get(name, "unknown")
            safe_print(f"Starting MCP server: {name}  (owner: {owner}) ...")

            server_env = cfg.get("env")
            merged_env = {**os.environ, **server_env} if server_env else None

            server_params = StdioServerParameters(
                command=cfg["command"],
                args=cfg.get("args", []),
                env=merged_env,
            )

            try:
                streams = await stack.enter_async_context(stdio_client(server_params))
                session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
            except Exception as exc:
                safe_print(f"  -> FAILED to start: {exc}")
                continue

            sessions[name] = session
            server_count += 1

            result = await session.list_tools()
            for tool in result.tools:
                ns = namespaced_name(name, tool.name)
                tool_owner[ns] = name
                tool_original[ns] = tool.name

            groq_tools.extend(mcp_tool_to_openai(name, t, owner) for t in result.tools)

            safe_print(f"  -> {len(result.tools)} tool(s) registered")

        # --- register local tools ---
        for tool_def in LOCAL_TOOL_DEFS:
            name = tool_def["function"]["name"]
            tool_owner[name] = LOCAL_SENTINEL
            tool_original[name] = name
            groq_tools.append(tool_def)

        # --- summary ---
        groq_tool_names = ", ".join(t["function"]["name"] for t in groq_tools)
        open_count = sum(1 for c in commitments if c.status == "open")
        unbought_count = sum(1 for i in shopping_items if not i.bought)
        safe_print(f"\nMCP Host ready  |  {server_count} server(s)  |  {len(groq_tools)} tool(s)")
        safe_print(f"Active: {active_label}  |  Open commitments: {open_count}  |  Shopping: {unbought_count} item(s)")

        # --- nudge summary ---
        nudge_text = generate_nudge_text(commitments, timezone)
        if nudge_text.strip():
            lines = nudge_text.split("\n")
            overdue = sum(1 for l in lines if "OVERDUE:" in l)
            due_today = sum(1 for l in lines if "DUE TODAY:" in l)
            due_soon = sum(1 for l in lines if "DUE SOON:" in l)
            parts = []
            if overdue:
                parts.append(f"{overdue} overdue")
            if due_today:
                parts.append(f"{due_today} due today")
            if due_soon:
                parts.append(f"{due_soon} due soon")
            safe_print(f"Nudges: {', '.join(parts)}")
        safe_print("Type a message, or 'exit' / 'quit' to stop.\n")

        # --- build calendar map context ---
        cal_hints = []
        for pid, cid in calendar_map.items():
            label = PERSON_LABELS.get(pid, pid)
            cal_hints.append(f"{label}'s calendar ID is \"{cid}\"")
        cal_context = "; ".join(cal_hints) if cal_hints else ""
        cal_suffix = f"\n\nCalendar mapping: {cal_context}\nWhen creating, editing, or viewing events for a person, use their calendar ID in the calendarId parameter." if cal_context else ""

        # --- system instruction ---
        system_content = (
            f"You are assisting {active_label}. "
            f"The user's timezone is {timezone}. "
            "When creating or displaying events, always use this timezone "
            "(set the timeZone field in dateTime objects). "
            "When showing calendar events, notes, files, or any data, "
            "make clear whose data it is (e.g. \"Divyanshu Garg's calendar shows...\" or "
            "\"dvinix has a note about...\"). "
            f"You have access to tools owned by different people. "
            "Only use tools owned by the person you are currently assisting unless the "
            "user explicitly asks you to access another person's data."
            f"{cal_suffix}"
            "\n\n"
            "--- Commitment extraction ---\n"
            "When the user says something that sounds like a commitment, promise, or task "
            "they'll handle (e.g. \"I'll handle...\", \"I've got...\", \"I'll deal with...\", "
            "\"someone needs to...\", \"I'll take care of...\", \"I'll do...\", "
            "\"I'll call...\", \"I'll pick up...\", \"I'll schedule...\", "
            "\"I got <something> covered\", \"I need to <do something>\"), "
            "call the record_commitment tool to save it.\n"
            "Extract the task description. The person responsible ALWAYS defaults to the "
            "active speaker — do NOT invent or guess any other name. "
            "Extract any deadline mentioned: day names (Monday/Tuesday/etc.), "
            "relative dates (\"this week\", \"by Friday\", \"tomorrow\", \"this weekend\"), "
            "or specific dates.\n"
            "Capture the original sentence as source_text.\n"
            "Do NOT ask the user to confirm — just silently record it and mention it in your reply."
        )

        # --- CLI loop ---
        messages: list[dict] = [{"role": "system", "content": system_content}]

        while True:
            try:
                user_input = input(f"{active_label}: ").strip()
            except (EOFError, KeyboardInterrupt):
                safe_print()
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            # Tag the message with who's speaking
            messages.append({
                "role": "user",
                "content": f"[{active_label}] {user_input}",
            })

            while True:
                response = await groq_chat_with_retry(
                    groq,
                    model="openai/gpt-oss-120b",
                    messages=messages,
                    tools=groq_tools or None,
                    tool_choice="auto",
                )

                choice = response.choices[0]
                msg = choice.message

                if not msg.tool_calls:
                    reply = msg.content or ""
                    save_commitments(commitments)
                    safe_print(f"\nAssistant: {reply}\n")
                    messages.append({"role": "assistant", "content": reply})
                    break

                # --- assistant message with tool_calls ---
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                })

                # --- execute each tool call ---
                for tc in msg.tool_calls:
                    ns_name = tc.function.name
                    orig_name = tool_original.get(ns_name)
                    srv_name = tool_owner.get(ns_name)

                    safe_print(f"  -> Calling tool: {ns_name}")

                    if srv_name is None:
                        result_text = f"Error: unknown tool '{ns_name}'"
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        })
                        continue

                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {}

                    # --- local tool handling ---
                    if srv_name == LOCAL_SENTINEL:
                        result_text = handle_local_tool(orig_name, args, commitments, shopping_items, active_person, active_label)
                        safe_print(f"  -> Local result: {result_text[:200]}")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        })
                        continue

                    # --- confirmation gate for MCP write operations ---
                    if is_write_tool(orig_name):
                        safe_print(f"\n  [!] Write operation: {orig_name}")
                        safe_print(f"      Args: {json.dumps(args, indent=2)}")
                        confirm = input("      Confirm? (y/N): ").strip().lower()
                        if confirm != "y":
                            result_text = "Execution cancelled by user"
                            safe_print(f"  -> Cancelled\n")
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result_text,
                            })
                            continue
                        safe_print()

                    # --- execute the MCP tool ---
                    session = sessions[srv_name]
                    try:
                        mcp_result = await session.call_tool(orig_name, args)
                        parts = []
                        for item in mcp_result.content:
                            if hasattr(item, "text"):
                                parts.append(item.text)
                            else:
                                parts.append(str(item))
                        result_text = "\n".join(parts)
                    except Exception as exc:
                        result_text = f"Error executing tool '{orig_name}': {exc}"

                    safe_print(
                        f"  -> Tool result ({orig_name}):"
                        f" {result_text[:200]}{'...' if len(result_text) > 200 else ''}"
                    )

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    })

    save_commitments(commitments)
    save_shopping_list(shopping_items)
    safe_print("Bye.")


if __name__ == "__main__":
    asyncio.run(main())

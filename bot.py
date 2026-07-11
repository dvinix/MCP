import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from groq import AsyncGroq
from contextlib import AsyncExitStack

# Import host logic
from common import (
    AppState, PERSON_LABELS, load_commitments, load_shopping_list, 
    save_commitments, save_shopping_list
)
from host import (
    load_config, setup_servers, build_tools, groq_chat_with_retry,
    is_write_tool, handle_local_tool, LOCAL_SENTINEL
)

load_dotenv()

# Map your Telegram User ID to the person ID here once you know it

TELEGRAM_USER_MAP = {
    8830550462: "dvinix"
}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text
    
    if not text:
        return

    if user_id not in TELEGRAM_USER_MAP:
        print(f"[Unknown user sent a message. Their Telegram ID is: {user_id}]")
        # Default to person_2 so the bot still works, but log it so the user can update the map
        person_id = "dvinix"
    else:
        person_id = TELEGRAM_USER_MAP[user_id]
        
    app_state = context.bot_data["app_state"]
    groq = context.bot_data["groq"]
    sessions = context.bot_data["sessions"]
    tool_owner = context.bot_data["tool_owner"]
    tool_original = context.bot_data["tool_original"]
    groq_tools = context.bot_data["groq_tools"]
    timezone = context.bot_data["timezone"]
    
    # We set active person per request based on who sent the message
    app_state.active_person = person_id
    app_state.active_label = PERSON_LABELS.get(person_id, person_id)
    active_label = app_state.active_label
    
    # Get or create chat history for this specific chat (handles DMs and Groups separately)
    if "messages" not in context.chat_data:
        cal_suffix = context.bot_data["cal_suffix"]
        system_content = (
            f"You are assisting users in a Telegram chat. "
            f"The user's timezone is {timezone}. "
            "When creating or displaying events, always use this timezone "
            "(set the timeZone field in dateTime objects). "
            "When showing calendar events, notes, files, or any data, "
            "make clear whose data it is. "
            f"You have access to tools owned by different people. "
            "Only use tools owned by the person you are currently assisting unless the "
            "user explicitly asks you to access another person's data."
            f"{cal_suffix}"
            "\n\n"
            "--- Commitment extraction ---\n"
            "When the user says something that sounds like a commitment, promise, or task "
            "they'll handle, call the record_commitment tool to save it.\n"
            "Extract the task description. The person responsible ALWAYS defaults to the "
            "active speaker — do NOT invent or guess any other name. "
            "Extract any deadline mentioned.\n"
            "Capture the original sentence as source_text.\n"
            "Do NOT ask the user to confirm — just silently record it and mention it in your reply."
        )
        context.chat_data["messages"] = [{"role": "system", "content": system_content}]
        
    messages = context.chat_data["messages"]
    
    messages.append({
        "role": "user",
        "content": f"[{active_label}] {text}",
    })
    
    try:
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
                if not reply.strip():
                    reply = "Done."
                save_commitments(app_state.commitments)
                save_shopping_list(app_state.shopping_items)
                messages.append({"role": "assistant", "content": reply})
                
                await update.message.reply_text(reply)
                break
                
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
            
            for tc in msg.tool_calls:
                ns_name = tc.function.name
                orig_name = tool_original.get(ns_name)
                srv_name = tool_owner.get(ns_name)
                
                if srv_name is None:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Error: unknown tool '{ns_name}'",
                    })
                    continue
                    
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                    
                if srv_name == LOCAL_SENTINEL:
                    result_text = handle_local_tool(orig_name, args, app_state)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    })
                    continue
                    
                # Removed the confirmation gate so write operations happen seamlessly.
                
                session = sessions[srv_name]
                try:
                    mcp_result = await asyncio.wait_for(session.call_tool(orig_name, args), timeout=30.0)
                    parts = []
                    for item in mcp_result.content:
                        if hasattr(item, "text"):
                            parts.append(item.text)
                        else:
                            parts.append(str(item))
                    result_text = "\n".join(parts)
                except Exception as exc:
                    result_text = f"Error executing tool '{orig_name}': {exc}"
                    
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text,
                })
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print("Crash in handle_message:", err)
        await update.message.reply_text(f"An internal error occurred:\n{e}")




async def post_init(application):
    print("Setting up MCP servers...")
    stack = AsyncExitStack()
    application.bot_data["stack"] = stack
    
    config = load_config()
    server_owners = {}
    for name, cfg in config["servers"].items():
        server_owners[name] = cfg.get("owner", "unknown")
        
    sessions, tool_owner, tool_original, mcp_tools, server_count = await setup_servers(config, server_owners, stack)
    groq_tools = build_tools(mcp_tools, tool_owner, tool_original)
    
    application.bot_data["sessions"] = sessions
    application.bot_data["tool_owner"] = tool_owner
    application.bot_data["tool_original"] = tool_original
    application.bot_data["groq_tools"] = groq_tools
    
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        print("Error: GROQ_API_KEY not set in .env")
        sys.exit(1)
        
    groq = AsyncGroq(api_key=api_key)
    application.bot_data["groq"] = groq
    
    app_state = AppState(
        commitments=load_commitments(),
        shopping_items=load_shopping_list(),
    )
    application.bot_data["app_state"] = app_state
    
    timezone = config.get("timezone", "UTC")
    application.bot_data["timezone"] = timezone
    
    calendar_map = config.get("calendar_map", {})
    cal_hints = []
    for pid, cid in calendar_map.items():
        label = PERSON_LABELS.get(pid, pid)
        cal_hints.append(f"{label}'s calendar ID is \"{cid}\"")
    cal_context = "; ".join(cal_hints) if cal_hints else ""
    cal_suffix = f"\n\nCalendar mapping: {cal_context}\nWhen creating, editing, or viewing events for a person, use their calendar ID in the calendarId parameter." if cal_context else ""
    application.bot_data["cal_suffix"] = cal_suffix
    
    print(f"MCP Host ready via Telegram | {server_count} servers | {len(groq_tools)} tools")

async def post_stop(application):
    stack = application.bot_data.get("stack")
    if stack:
        await stack.aclose()


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)
        
    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("Bot polling started...")
    application.run_polling()

if __name__ == "__main__":
    main()

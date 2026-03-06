#!/usr/bin/env python3
"""
ChatGPT Export Parser for LocalAgentViewer.

Parses OpenAI's conversations.json export (monolithic JSON array) into the
unified SQLite database as source "chatgpt", host "cloud".

Key differences from JSONL parsers:
  - Single JSON file (~634MB, 6k+ interactions)
  - Messages stored as a DAG tree (parent/children), not a flat list
  - Timestamps are Unix epoch floats (not ISO 8601)
  - No token usage data in export
  - Tool tracking via author.role == "tool" with author.name
"""

import argparse
import getpass
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from lav.config import (
    SOURCE_CHATGPT,
    CHATGPT_EXPORT_PATH,
    get_chatgpt_export_path,
)
from lav.parsers.jsonl import (
    init_db,
    get_or_create_project,
    get_or_create_user,
    get_or_create_host,
    get_parse_state,
    set_parse_state,
    upsert_session_source,
    smart_title,
)


# ===========================================================================
# SESSION ID PREFIX
# ===========================================================================

def format_chatgpt_session_id(conversation_id: str) -> str:
    """Prefix ChatGPT interaction IDs to avoid collisions."""
    if not conversation_id:
        return ""
    if conversation_id.startswith("chatgpt:"):
        return conversation_id
    return f"chatgpt:{conversation_id}"


# ===========================================================================
# PROJECT MAPPING
# ===========================================================================

def resolve_chatgpt_project(conv: dict) -> str:
    """Determine project name from gizmo_type/gizmo_id.

    - snorlax = ChatGPT Project
    - gpt = Custom GPT
    - None = generic chat
    """
    gizmo_type = conv.get("gizmo_type")
    gizmo_id = conv.get("gizmo_id")

    if gizmo_type == "snorlax" and gizmo_id:
        return f"chatgpt:{gizmo_id}"
    elif gizmo_type == "gpt" and gizmo_id:
        return f"chatgpt:gpt:{gizmo_id}"
    return "chatgpt"


# ===========================================================================
# TREE LINEARIZATION
# ===========================================================================

def linearize_tree(mapping: dict, current_node: str) -> list[dict]:
    """Walk from current_node up to root, reverse for chronological order.

    Returns list of message dicts (skipping nodes with no message or system hidden).
    """
    path_ids = []
    node_id = current_node
    while node_id:
        node = mapping.get(node_id)
        if not node:
            break
        path_ids.append(node_id)
        node_id = node.get("parent")

    path_ids.reverse()

    messages = []
    for nid in path_ids:
        node = mapping[nid]
        msg = node.get("message")
        if not msg:
            continue
        role = msg.get("author", {}).get("role", "")
        # Skip system messages (hidden context)
        if role == "system":
            continue
        messages.append(msg)

    return messages


# ===========================================================================
# TOOL EXTRACTION
# ===========================================================================

def extract_chatgpt_tools(mapping: dict) -> list[str]:
    """Scan all nodes for tool messages and return unique tool names."""
    tools = set()
    for node in mapping.values():
        msg = node.get("message")
        if not msg:
            continue
        author = msg.get("author", {})
        if author.get("role") == "tool":
            name = author.get("name", "")
            if name:
                tools.add(name)
    return sorted(tools)


# ===========================================================================
# CONTENT EXTRACTION
# ===========================================================================

def extract_text_from_parts(parts: list) -> str:
    """Extract text content from message parts, skipping non-string items."""
    texts = []
    for part in parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            # Some parts are dicts (e.g. image references) - skip
            text = part.get("text", "")
            if text:
                texts.append(text)
    return "\n".join(texts)


def extract_message_text(msg: dict) -> str:
    """Extract text from a ChatGPT message, handling all content_type variants.

    Content types:
      - text: standard message, content in parts[]
      - thoughts: reasoning steps, content in thoughts[].content
      - reasoning_recap: thinking summary (e.g. "Ragionato per 35s"), in content field
      - code: internal tool call JSON, in text field
    """
    content = msg.get("content", {})
    ct = content.get("content_type", "text")

    if ct == "text":
        parts = content.get("parts", [])
        return extract_text_from_parts(parts)

    if ct == "thoughts":
        thoughts = content.get("thoughts", [])
        pieces = []
        for t in thoughts:
            summary = t.get("summary", "")
            body = t.get("content", "")
            if summary and body:
                pieces.append(f"**{summary}**\n{body}")
            elif body:
                pieces.append(body)
            elif summary:
                pieces.append(summary)
        return "\n\n".join(pieces) if pieces else ""

    if ct == "reasoning_recap":
        return content.get("content", "")

    if ct == "code":
        text = content.get("text", "")
        lang = content.get("language", "")
        if text:
            return f"```{lang}\n{text}\n```"
        return ""

    # Fallback: try parts, then text
    parts = content.get("parts", [])
    if parts:
        return extract_text_from_parts(parts)
    return content.get("text", "") or content.get("content", "") or ""


def epoch_to_iso(epoch: Optional[float]) -> str:
    """Convert Unix epoch float to ISO 8601 string."""
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch).isoformat()
    except (OSError, ValueError, OverflowError):
        return ""


# ===========================================================================
# MAIN PARSE FUNCTION
# ===========================================================================

def parse_chatgpt_export(
    filepath: Path,
    conn: sqlite3.Connection,
    full: bool = False,
    user: str = None,
    host: str = "cloud",
) -> dict:
    """Parse ChatGPT conversations.json into the unified DB.

    Args:
        filepath: Path to conversations.json
        conn: SQLite connection (write)
        full: Force full reparse (ignore incremental state)
        user: Username to assign (default: current OS user)
        host: Host name to assign (default: cloud)

    Returns:
        Stats dict with counts.
    """
    # NOTE: "conversation_id", "conversations.json" are OpenAI's external
    # schema keys and must not be renamed.
    user = user or getpass.getuser()
    if not filepath.exists():
        print(f"ChatGPT export not found: {filepath}")
        return {"error": f"File not found: {filepath}"}

    print(f"\nParsing ChatGPT export: {filepath}")
    print(f"  User: {user}, Host: {host}")

    # Resolve user/host IDs
    user_id = get_or_create_user(conn, user)
    host_id = get_or_create_host(conn, host, os_type="cloud", home_dir="")

    # Get incremental state: last update_time we processed
    # Use a global key (not per-project) since ChatGPT is one big file
    last_update = None if full else get_parse_state(
        conn, "chatgpt_last_update", -1, SOURCE_CHATGPT, host_id
    )
    if last_update:
        last_update_float = float(last_update)
        print(f"  Incremental from update_time: {last_update} ({epoch_to_iso(last_update_float)})")
    else:
        last_update_float = 0.0
        print("  Full parse")

    # Load JSON
    print("  Loading JSON...")
    with open(filepath, "r", encoding="utf-8") as f:
        interactions = json.load(f)
    print(f"  Loaded {len(interactions)} interactions")

    max_update_time = last_update_float
    interactions_processed = 0
    interactions_skipped = 0
    messages_inserted = 0
    tool_calls_inserted = 0

    for conv in interactions:
        update_time = conv.get("update_time") or 0.0
        # Skip if not newer than last parse
        if update_time <= last_update_float:
            interactions_skipped += 1
            continue

        conversation_id = conv.get("conversation_id", "")
        if not conversation_id:
            continue

        session_id = format_chatgpt_session_id(conversation_id)
        title = conv.get("title", "")
        create_time = conv.get("create_time") or 0.0
        default_model = conv.get("default_model_slug", "")
        mapping = conv.get("mapping", {})
        current_node = conv.get("current_node", "")

        # Track max update_time for incremental
        if update_time > max_update_time:
            max_update_time = update_time

        # Resolve project
        project_name = resolve_chatgpt_project(conv)
        project_id = get_or_create_project(conn, project_name)

        # Upsert session source
        upsert_session_source(
            conn, session_id, project_id, SOURCE_CHATGPT,
            meta={
                "gizmo_type": conv.get("gizmo_type"),
                "gizmo_id": conv.get("gizmo_id"),
                "default_model_slug": default_model,
            },
        )

        # Linearize tree
        if not current_node or not mapping:
            # No messages to process, just create interaction record
            timestamp = epoch_to_iso(create_time)
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO interactions
                       (session_id, project_id, user_id, host_id, timestamp,
                        display, summary, project, model, total_tokens,
                        message_count, tools_used, cwd, git_branch,
                        parent_session_id, agent_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '[]', '', '', NULL, NULL)""",
                    (session_id, project_id, user_id, host_id, timestamp,
                     title[:200] if title else "", title or "(no title)", project_name, default_model),
                )
            except sqlite3.Error as e:
                print(f"  DB error (empty interaction): {e}")
            interactions_processed += 1
            continue

        messages = linearize_tree(mapping, current_node)

        # Insert messages
        first_user_text = ""
        msg_count = 0
        interaction_model = default_model

        for msg in messages:
            role = msg.get("author", {}).get("role", "")
            if role not in ("user", "assistant", "tool"):
                continue

            # Map role to LAV type
            if role == "tool":
                # Track tool usage but don't insert as a message
                tool_name = msg.get("author", {}).get("name", "")
                if tool_name:
                    msg_ts = epoch_to_iso(msg.get("create_time"))
                    try:
                        conn.execute(
                            """INSERT OR IGNORE INTO mcp_tool_calls
                               (timestamp, session_id, project_id, user_id, host_id,
                                tool_name, server_name, cwd, git_branch)
                               VALUES (?, ?, ?, ?, ?, ?, 'chatgpt', '', '')""",
                            (msg_ts or epoch_to_iso(create_time), session_id, project_id,
                             user_id, host_id, tool_name),
                        )
                        tool_calls_inserted += 1
                    except sqlite3.Error:
                        pass
                continue

            msg_type = role  # "user" or "assistant"
            text = extract_message_text(msg)
            content_type = msg.get("content", {}).get("content_type", "text")
            msg_ts = epoch_to_iso(msg.get("create_time"))
            model = msg.get("metadata", {}).get("model_slug", "")

            # Skip empty messages (streaming artifacts with no content)
            if not text.strip():
                continue

            if model:
                interaction_model = model

            # Capture first user text for display
            if msg_type == "user" and not first_user_text and text.strip():
                first_user_text = text.strip()

            # Generate a stable UUID from interaction's conversation_id + message position
            msg_id = msg.get("id", "")
            uuid = f"chatgpt:{hashlib.sha1((conversation_id + '|' + msg_id).encode()).hexdigest()}"

            try:
                conn.execute(
                    """INSERT OR REPLACE INTO messages
                       (session_id, project_id, user_id, host_id, uuid,
                        type, content, timestamp, tokens_in, tokens_out, model)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)""",
                    (session_id, project_id, user_id, host_id, uuid,
                     msg_type, text, msg_ts, model),
                )
                messages_inserted += 1
                msg_count += 1
            except sqlite3.Error as e:
                print(f"  DB error (message): {e}")

        # Extract tools used
        tools = extract_chatgpt_tools(mapping)
        tools_json = json.dumps(tools) if tools else "[]"

        # Build summary
        summary = title if title else smart_title(first_user_text)
        display = first_user_text[:200] if first_user_text else (title[:200] if title else "")
        timestamp = epoch_to_iso(create_time)

        # Upsert interaction
        try:
            conn.execute(
                """INSERT OR REPLACE INTO interactions
                   (session_id, project_id, user_id, host_id, timestamp,
                    display, summary, project, model, total_tokens,
                    message_count, tools_used, cwd, git_branch,
                    parent_session_id, agent_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, '', '', NULL, NULL)""",
                (session_id, project_id, user_id, host_id, timestamp,
                 display, summary, project_name, interaction_model, msg_count, tools_json),
            )
        except sqlite3.Error as e:
            print(f"  DB error (interaction): {e}")

        interactions_processed += 1

        # Commit every 500 interactions for crash resilience
        if interactions_processed % 500 == 0:
            conn.commit()
            print(f"  Progress: {interactions_processed} interactions processed...")

    # Final commit
    conn.commit()

    # Update parse state
    if max_update_time > last_update_float:
        set_parse_state(conn, "chatgpt_last_update", str(max_update_time), -1, SOURCE_CHATGPT, host_id)
        conn.commit()

    stats = {
        "source": SOURCE_CHATGPT,
        "interactions_processed": interactions_processed,
        "interactions_skipped": interactions_skipped,
        "messages_inserted": messages_inserted,
        "tool_calls_inserted": tool_calls_inserted,
    }

    print(f"  Done: {interactions_processed} interactions, {messages_inserted} messages, {tool_calls_inserted} tool calls")
    if interactions_skipped:
        print(f"  Skipped (already parsed): {interactions_skipped}")

    return stats


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Parse ChatGPT export (conversations.json) into LocalAgentViewer DB"
    )
    parser.add_argument("--full", "-f", action="store_true", help="Force full reparse")
    parser.add_argument("--file", type=str, help="Path to conversations.json (overrides config)")
    parser.add_argument("--user", type=str, default=None, help="Username (default: current OS user)")
    parser.add_argument("--host", type=str, default="cloud", help="Host name (default: cloud)")
    args = parser.parse_args()

    filepath = Path(args.file) if args.file else get_chatgpt_export_path()
    if not filepath:
        print("No ChatGPT export path configured. Use --file or set in settings.local.json")
        return

    conn = init_db()
    print(f"Database: {conn.execute('PRAGMA database_list').fetchone()[2]}")

    stats = parse_chatgpt_export(filepath, conn, full=args.full, user=args.user, host=args.host)

    conn.close()
    print(f"\nDone.")


if __name__ == "__main__":
    main()

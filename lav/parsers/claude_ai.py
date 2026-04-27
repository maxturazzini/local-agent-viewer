#!/usr/bin/env python3
"""
Claude.ai Export Parser for LocalAgentViewer.

Parses Anthropic's on-demand account export folder (data-<uuid>-...-batch-0000/)
into the unified SQLite database as source "claude_ai", host "cloud".

Scope (v1):
  - conversations.json: 1 row in interactions per conv, N rows in messages,
    M rows in mcp_tool_calls (one per tool_use content block).
  - users.json: read for informational logging only.
  - projects.json / memories.json / design_chats/: out of scope.

Differences vs other parsers:
  - Single monolithic JSON (like chatgpt) — but timestamps are ISO 8601.
  - chat_messages is already a flat chronological list (no DAG).
  - content[] has 6 types: text, thinking, tool_use, tool_result,
    token_budget, voice_note. The flat msg.text field is NOT authoritative;
    we render content[] in order.
  - No token usage and no model slug in the export.
"""

import argparse
import getpass
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from lav.config import SOURCE_CLAUDE_AI, get_claudeai_export_folder
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


PROJECT_NAME = "claude_ai"
TOOL_USE_INPUT_CAP = 2000
TOOL_RESULT_CAP = 5000


def format_session_id(conv_uuid: str) -> str:
    if not conv_uuid:
        return ""
    return conv_uuid if conv_uuid.startswith("claudeai:") else f"claudeai:{conv_uuid}"


def message_uuid(conv_uuid: str, msg_uuid: str) -> str:
    h = hashlib.sha1(f"{conv_uuid}|{msg_uuid}".encode()).hexdigest()
    return f"claudeai:{h}"


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _flatten_tool_result_content(content) -> str:
    """tool_result.content is a list of {type, text, ...} blocks."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict):
            t = block.get("text") or block.get("content") or ""
            if isinstance(t, (dict, list)):
                t = json.dumps(t, ensure_ascii=False)
            if t:
                parts.append(str(t))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _render_content_block(cont: dict) -> str:
    t = cont.get("type")
    if t == "text":
        return cont.get("text", "") or ""
    if t == "thinking":
        body = cont.get("thinking", "") or ""
        return f"\n--- thinking ---\n{body}\n" if body else ""
    if t == "tool_use":
        name = cont.get("name") or "tool"
        raw = cont.get("input")
        try:
            payload = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = str(raw)
        if len(payload) > TOOL_USE_INPUT_CAP:
            payload = payload[:TOOL_USE_INPUT_CAP] + " ... [truncated]"
        return f"\n--- tool_use: {name} ---\n{payload}\n"
    if t == "tool_result":
        name = cont.get("name") or "tool"
        status = "error" if cont.get("is_error") else "ok"
        body = _flatten_tool_result_content(cont.get("content"))
        if len(body) > TOOL_RESULT_CAP:
            body = body[:TOOL_RESULT_CAP] + " ... [truncated]"
        return f"\n--- tool_result: {name} ({status}) ---\n{body}\n"
    if t == "voice_note":
        title = cont.get("title") or ""
        text = cont.get("text") or ""
        header = f"voice note: {title}" if title else "voice note"
        return f"\n--- {header} ---\n{text}\n"
    # token_budget and any unknown type: skip
    return ""


def extract_message_text(msg: dict) -> str:
    """Render full ordered content (text + thinking + tools + voice) plus
    attachments (with extracted_content) and file references."""
    pieces = []
    for cont in msg.get("content", []) or []:
        rendered = _render_content_block(cont)
        if rendered:
            pieces.append(rendered)

    body = "".join(pieces).strip()
    if not body and msg.get("text"):
        # Fallback: pre-content-format messages (older exports) only have flat text
        body = msg["text"]

    extras = []
    for a in msg.get("attachments", []) or []:
        name = a.get("file_name") or "attachment"
        ftype = a.get("file_type") or ""
        fsize = a.get("file_size")
        extracted = a.get("extracted_content") or ""
        header = f"attachment: {name}"
        meta = []
        if ftype:
            meta.append(ftype)
        if fsize is not None:
            meta.append(f"{fsize}B")
        if meta:
            header = f"{header} ({', '.join(meta)})"
        extras.append(f"\n--- {header} ---\n{extracted}")

    files = msg.get("files", []) or []
    if files:
        names = [f.get("file_name", "?") for f in files]
        extras.append(f"\n--- file references: {', '.join(names)} ---")

    return (body + "".join(extras)).strip()


def first_user_message_text(messages: list[dict]) -> str:
    for m in messages:
        if m.get("sender") == "human":
            text = extract_message_text(m).strip()
            if text:
                return text
    return ""


def parse_claudeai_export(
    folder: Path,
    conn: sqlite3.Connection,
    full: bool = False,
    user: Optional[str] = None,
    host: str = "cloud",
) -> dict:
    user = user or getpass.getuser()
    folder = Path(folder)
    conv_path = folder / "conversations.json"
    if not conv_path.exists():
        print(f"conversations.json not found in: {folder}")
        return {"error": f"File not found: {conv_path}"}

    print(f"\nParsing claude.ai export: {folder}")
    print(f"  User: {user}, Host: {host}")

    users_path = folder / "users.json"
    if users_path.exists():
        try:
            with open(users_path, "r", encoding="utf-8") as f:
                accounts = json.load(f) or []
            for acc in accounts:
                print(f"  Account: {acc.get('full_name','?')} <{acc.get('email_address','?')}> ({acc.get('uuid','?')})")
        except (OSError, ValueError):
            pass

    user_id = get_or_create_user(conn, user)
    host_id = get_or_create_host(conn, host, os_type="cloud", home_dir="")
    project_id = get_or_create_project(conn, PROJECT_NAME)

    # Incremental cursor on conv.updated_at (ISO string is monotonic for same TZ suffix,
    # but we parse to datetime for safety).
    last_state = None if full else get_parse_state(
        conn, "claude_ai_last_update", -1, SOURCE_CLAUDE_AI, host_id
    )
    last_dt = parse_iso(last_state) if last_state else None
    if last_dt:
        print(f"  Incremental from updated_at: {last_state}")
    else:
        print("  Full parse")
        # mcp_tool_calls has no UNIQUE constraint, so INSERT OR IGNORE doesn't
        # dedupe on re-runs. Wipe prior claude_ai rows before re-inserting.
        try:
            conn.execute(
                "DELETE FROM mcp_tool_calls WHERE session_id LIKE 'claudeai:%'"
            )
            conn.commit()
        except sqlite3.Error as e:
            print(f"  DB warning (cleanup mcp_tool_calls): {e}")

    print("  Loading JSON...")
    with open(conv_path, "r", encoding="utf-8") as f:
        conversations = json.load(f)
    print(f"  Loaded {len(conversations)} conversations")

    max_dt = last_dt
    max_iso = last_state or ""
    interactions_processed = 0
    interactions_skipped = 0
    messages_inserted = 0
    tool_calls_inserted = 0

    for conv in conversations:
        updated_at = conv.get("updated_at") or conv.get("created_at") or ""
        upd_dt = parse_iso(updated_at)
        if last_dt and upd_dt and upd_dt <= last_dt:
            interactions_skipped += 1
            continue
        if upd_dt and (max_dt is None or upd_dt > max_dt):
            max_dt = upd_dt
            max_iso = updated_at

        conv_uuid = conv.get("uuid", "")
        if not conv_uuid:
            continue

        session_id = format_session_id(conv_uuid)
        title = conv.get("name") or ""
        created_at = conv.get("created_at") or ""
        chat_messages = conv.get("chat_messages", []) or []
        account_uuid = (conv.get("account") or {}).get("uuid", "")

        upsert_session_source(
            conn, session_id, project_id, SOURCE_CLAUDE_AI,
            meta={
                "name": title or None,
                "account_uuid": account_uuid or None,
            },
        )

        msg_count = 0
        tool_names: set[str] = set()
        first_user_text = ""

        for msg in chat_messages:
            sender = msg.get("sender")
            if sender not in ("human", "assistant"):
                continue
            msg_id = msg.get("uuid", "")
            if not msg_id:
                continue

            text = extract_message_text(msg)
            if not text:
                continue

            if sender == "human" and not first_user_text:
                first_user_text = text

            msg_type = "user" if sender == "human" else "assistant"
            msg_ts = msg.get("created_at") or created_at
            uuid = message_uuid(conv_uuid, msg_id)

            try:
                conn.execute(
                    """INSERT OR REPLACE INTO messages
                       (session_id, project_id, user_id, host_id, uuid,
                        type, content, timestamp, tokens_in, tokens_out, model)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '')""",
                    (session_id, project_id, user_id, host_id, uuid,
                     msg_type, text, msg_ts),
                )
                messages_inserted += 1
                msg_count += 1
            except sqlite3.Error as e:
                print(f"  DB error (message {uuid}): {e}")
                continue

            # Tool calls (only assistant emits tool_use)
            if sender == "assistant":
                for cont in msg.get("content", []) or []:
                    if cont.get("type") != "tool_use":
                        continue
                    tname = cont.get("name") or ""
                    if not tname:
                        continue
                    tool_names.add(tname)
                    integration = cont.get("integration_name") or ""
                    server = integration if integration else "claude_ai"
                    tts = cont.get("start_timestamp") or msg_ts
                    try:
                        conn.execute(
                            """INSERT OR IGNORE INTO mcp_tool_calls
                               (timestamp, session_id, project_id, user_id, host_id,
                                tool_name, server_name, cwd, git_branch)
                               VALUES (?, ?, ?, ?, ?, ?, ?, '', '')""",
                            (tts, session_id, project_id, user_id, host_id,
                             tname, server),
                        )
                        tool_calls_inserted += 1
                    except sqlite3.Error:
                        pass

        summary = title or conv.get("summary") or smart_title(first_user_text)
        display = (first_user_text or title or "")[:200]
        tools_json = json.dumps(sorted(tool_names))

        try:
            conn.execute(
                """INSERT OR REPLACE INTO interactions
                   (session_id, project_id, user_id, host_id, timestamp,
                    display, summary, project, model, total_tokens,
                    message_count, tools_used, cwd, git_branch,
                    parent_session_id, agent_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, ?, '', '', NULL, NULL)""",
                (session_id, project_id, user_id, host_id, created_at,
                 display, summary, PROJECT_NAME, msg_count, tools_json),
            )
        except sqlite3.Error as e:
            print(f"  DB error (interaction {session_id}): {e}")

        interactions_processed += 1
        if interactions_processed % 500 == 0:
            conn.commit()
            print(f"  Progress: {interactions_processed} conversations processed...")

    conn.commit()

    if max_iso and (not last_state or max_iso > last_state):
        set_parse_state(
            conn, "claude_ai_last_update", max_iso, -1, SOURCE_CLAUDE_AI, host_id
        )
        conn.commit()

    stats = {
        "source": SOURCE_CLAUDE_AI,
        "interactions_processed": interactions_processed,
        "interactions_skipped": interactions_skipped,
        "messages_inserted": messages_inserted,
        "tool_calls_inserted": tool_calls_inserted,
    }
    print(f"  Done: {interactions_processed} conversations, {messages_inserted} messages, {tool_calls_inserted} tool calls")
    if interactions_skipped:
        print(f"  Skipped (already parsed): {interactions_skipped}")
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Parse Anthropic claude.ai account export folder into LocalAgentViewer DB"
    )
    parser.add_argument("--folder", type=str, default=None,
                        help="Path to export folder (data-*-batch-0000). Auto-discovered in ~/Downloads if omitted.")
    parser.add_argument("--full", "-f", action="store_true", help="Force full reparse")
    parser.add_argument("--user", type=str, default=None, help="Username (default: current OS user)")
    parser.add_argument("--host", type=str, default="cloud", help="Host name (default: cloud)")
    args = parser.parse_args()

    folder = get_claudeai_export_folder(args.folder)
    if not folder:
        print("No claude.ai export folder found. Use --folder or set CLAUDE_AI_EXPORT_PATH.")
        return
    print(f"Export folder: {folder}")

    conn = init_db()
    print(f"Database: {conn.execute('PRAGMA database_list').fetchone()[2]}")
    parse_claudeai_export(folder, conn, full=args.full, user=args.user, host=args.host)
    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()

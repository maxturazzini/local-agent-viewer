#!/usr/bin/env python3
"""
LocalAgentViewer API Server

ThreadingHTTPServer with read-only connections for queries and
write connection for sync operations. Serves JSON data from
the unified SQLite database.
"""

# Load .env file if present (for API keys)
from pathlib import Path as _Path
_env_file = _Path(__file__).parent / ".env"
if _env_file.exists():
    import os
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()

import getpass
import io
import json
import socket
import sqlite3
import threading
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import urlparse, parse_qs, unquote

from config import (
    UNIFIED_DB_PATH,
    QDRANT_DATA_DIR,
    QDRANT_COLLECTION,
    QDRANT_URL,
    PORT,
    SOURCE_CLAUDE_CODE,
    SOURCE_CODEX_CLI,
    SOURCE_COWORK_DESKTOP,
    SOURCE_CHATGPT,
    get_claude_projects_dirs,
    get_chatgpt_export_path,
    load_runtime_config,
)
from parser import (
    init_db,
    parse_project,
    parse_codex_sessions,
    parse_cowork_sessions,
    extract_project_name,
    parse_jsonl_file,
    process_message_content,
    update_conversation,
    get_or_create_project,
    get_or_create_user,
    get_or_create_host,
    detect_user_from_path,
    detect_host_from_path,
    get_parse_state,
    set_parse_state,
    ingest_remote_sessions,
)
from parser_chatgpt import parse_chatgpt_export
from queries import (
    run_query,
    build_filters,
    get_token_stats,
    get_files_stats,
    get_skills_stats,
    get_subagents_stats,
    get_mcp_stats,
    get_bash_stats,
    get_searches_stats,
    get_client_stats,
    get_timeline_stats,
    get_date_range,
    get_conversations_list,
    search_messages,
    get_conversation_detail,
    get_users_list,
    get_hosts_list,
    get_projects_list,
    get_user_detail,
    export_sessions,
    get_conversation_metadata,
    get_classification_stats,
    get_tagcloud_data,
)

# Runtime config (agent/collector roles)
_runtime_config = load_runtime_config()
_server_start_time = datetime.now()

# Lazy-loaded Qdrant components
_kb_store = None
_kb_indexer = None

# Sync lock for write operations
_sync_lock = threading.Lock()
_sync_status = {
    "running": False,
    "scope": None,
    "progress": None,
    "started": None,
    "last_completed": None,
}


def get_kb_store(require_openai: bool = True):
    """Get or create the KB vector store (HTTP or file mode)."""
    global _kb_store
    if _kb_store is None:
        import os
        if not require_openai and not os.getenv("OPENAI_API_KEY"):
            return None
        from qdrant.store import ConversationVectorStore
        if QDRANT_URL:
            _kb_store = ConversationVectorStore(url=QDRANT_URL, collection=QDRANT_COLLECTION)
        else:
            QDRANT_DATA_DIR.mkdir(parents=True, exist_ok=True)
            _kb_store = ConversationVectorStore(data_path=QDRANT_DATA_DIR, collection=QDRANT_COLLECTION)
        _kb_store.ensure_collection()
    return _kb_store


def get_kb_indexer():
    """Get or create the KB indexer (lazy initialization)."""
    global _kb_indexer
    if _kb_indexer is None:
        from qdrant.indexer import ConversationIndexer
        _kb_indexer = ConversationIndexer(get_kb_store())
    return _kb_indexer


# ===========================================================================
# DB CONNECTIONS
# ===========================================================================

def get_read_connection() -> Optional[sqlite3.Connection]:
    """Get a read-only connection to the unified DB."""
    if not UNIFIED_DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_write_connection() -> sqlite3.Connection:
    """Get a write connection to the unified DB (creates if needed)."""
    conn = init_db(UNIFIED_DB_PATH)
    return conn


# ===========================================================================
# RESOLVE IDS from query params
# ===========================================================================

def resolve_ids(conn, params):
    """Resolve project/user/host query params to integer IDs.

    Accepts both names (project=miniMe) and IDs (project_id=5).
    Returns dict with project_id, user_id, host_id (or None).
    """
    result = {
        "project_id": None,
        "user_id": None,
        "host_id": None,
    }

    # Project
    pid = params.get("project_id", [None])[0]
    if pid:
        result["project_id"] = int(pid)
    else:
        pname = params.get("project", [None])[0]
        if pname:
            row = run_query(conn, "SELECT id FROM projects WHERE name = ?", [pname])
            if row:
                result["project_id"] = row[0]["id"]

    # User
    uid = params.get("user_id", [None])[0]
    if uid:
        result["user_id"] = int(uid)
    else:
        uname = params.get("user", [None])[0]
        if uname:
            row = run_query(conn, "SELECT id FROM users WHERE username = ?", [uname])
            if row:
                result["user_id"] = row[0]["id"]

    # Host
    hid = params.get("host_id", [None])[0]
    if hid:
        result["host_id"] = int(hid)
    else:
        hname = params.get("host", [None])[0]
        if hname:
            row = run_query(conn, "SELECT id FROM hosts WHERE hostname = ?", [hname])
            if row:
                result["host_id"] = row[0]["id"]

    return result


# ===========================================================================
# PULL FROM REMOTE AGENTS
# ===========================================================================

def pull_from_agents(conn, agents_config: list, agent_filter: str = None, full: bool = False) -> dict:
    """Pull data from configured remote agents via /api/export.

    Args:
        agent_filter: If set, only pull from the agent with this name.
        full: If True, pull all history (since=epoch) ignoring last_pull state.
    """
    import urllib.request
    results = {}
    for agent in agents_config:
        name = agent["name"]

        if agent_filter and name != agent_filter:
            continue

        last_pull = get_parse_state(conn, f"last_pull:{name}", project_id=-1, source="remote", host_id=-1)
        since = "1970-01-01T00:00:00" if full or not last_pull else last_pull

        urls = [agent["url"]]
        if "fallback_url" in agent:
            urls.append(agent["fallback_url"])

        timeout = agent.get("timeout_seconds", 10)
        success = False
        print(f"[pull] Agent '{name}' — since={since}")
        for url in urls:
            try:
                export_url = f"{url}/api/export?since={since}"
                resp = urllib.request.urlopen(export_url, timeout=timeout)
                raw = resp.read()
                package = json.loads(raw)
                stats = ingest_remote_sessions(
                    conn,
                    package.get("sessions", []),
                    package.get("host", {}),
                    package.get("user", {}),
                )
                set_parse_state(conn, f"last_pull:{name}",
                                datetime.now().isoformat(),
                                project_id=-1, source="remote", host_id=-1)
                conn.commit()
                print(f"[pull] Agent '{name}' OK — {stats}")
                results[name] = {"status": "ok", "url": url, "since": since, **stats}
                success = True
                break
            except Exception as e:
                print(f"[pull] Agent '{name}' ERROR at {url}: {e}")
                results[name] = {"status": "error", "url": url, "error": str(e)}
                continue
        if not success:
            print(f"[pull] Failed to reach agent '{name}' at any URL")
    return results


# ===========================================================================
# AUTO-CLASSIFICATION (post-sync)
# ===========================================================================

def _auto_classify_new(conv_ids: set = None) -> int:
    """Classify new conversations using OpenAI gpt-4.1-mini.

    Called automatically after sync with the set of (session_id, project_id)
    tuples that were added during this sync. Only classifies those.
    Skips silently if OPENAI_API_KEY is not set.
    """
    import os
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("[classify] Skipped — OPENAI_API_KEY not set")
        return 0

    if not conv_ids:
        return 0

    try:
        read_conn = sqlite3.connect(str(UNIFIED_DB_PATH))
        read_conn.row_factory = sqlite3.Row
        read_conn.execute("PRAGMA journal_mode=WAL")
        read_conn.execute("PRAGMA busy_timeout=5000")
        read_conn.execute("PRAGMA query_only=ON")

        # Filter to only unclassified ones with >= 2 messages
        placeholders = ",".join(["(?,?)"] * len(conv_ids))
        flat_params = []
        for sid, pid in conv_ids:
            flat_params.extend([sid, pid])

        candidates = read_conn.execute(f"""
            SELECT c.session_id, c.project_id, c.message_count
            FROM conversations c
            LEFT JOIN conversation_metadata cm
                ON cm.session_id = c.session_id AND cm.project_id = c.project_id
            WHERE cm.session_id IS NULL
              AND c.message_count >= 2
              AND (c.session_id, c.project_id) IN ({placeholders})
            ORDER BY c.timestamp DESC
        """, flat_params).fetchall()

        if not candidates:
            read_conn.close()
            return 0

        print(f"[classify] {len(candidates)} new conversations to classify")
        _sync_status["progress"] = f"Classifying {len(candidates)} conversations..."

        import openai
        from classifiers.openai_classifier import classify_conversation

        client = openai.OpenAI(api_key=api_key)
        write_conn = init_db(UNIFIED_DB_PATH)
        model = "gpt-4.1-mini"
        classified = 0

        for conv in candidates:
            sid, pid = conv["session_id"], conv["project_id"]
            try:
                rows = read_conn.execute(
                    "SELECT type, content FROM messages WHERE session_id = ? AND project_id = ? ORDER BY id",
                    (sid, pid),
                ).fetchall()
                messages = [{"type": r["type"], "content": r["content"]} for r in rows]
                if not messages:
                    continue

                metadata = classify_conversation(messages, client, model=model)

                now = datetime.now().isoformat()
                write_conn.execute("""
                    INSERT INTO conversation_metadata
                        (session_id, project_id, summary, abstract, process, classification,
                         data_sensitivity, sensitive_data_types, topics, people, clients,
                         tags, model_used, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, project_id) DO UPDATE SET
                        summary=excluded.summary, abstract=excluded.abstract,
                        process=excluded.process, classification=excluded.classification,
                        data_sensitivity=excluded.data_sensitivity,
                        sensitive_data_types=excluded.sensitive_data_types,
                        topics=excluded.topics, people=excluded.people, clients=excluded.clients,
                        model_used=excluded.model_used, updated_at=excluded.updated_at
                """, (
                    sid, pid,
                    metadata.get("summary", ""), metadata.get("abstract", ""),
                    metadata.get("process", ""), metadata.get("classification", "development"),
                    metadata.get("data_sensitivity", "internal"),
                    json.dumps(metadata.get("sensitive_data_types", [])),
                    json.dumps(metadata.get("topics", [])),
                    json.dumps(metadata.get("people", [])),
                    json.dumps(metadata.get("clients", [])),
                    "[]", model, now, now,
                ))
                write_conn.commit()
                classified += 1
                cls = metadata.get("classification", "?")
                print(f"[classify] {sid[:8]} → {cls}")

            except Exception as e:
                print(f"[classify] {sid[:8]} ERROR: {e}")

        read_conn.close()
        write_conn.close()
        print(f"[classify] Done — {classified}/{len(candidates)} classified")
        return classified

    except Exception as e:
        print(f"[classify] Fatal error: {e}")
        return 0


# ===========================================================================
# SYNC ENGINE
# ===========================================================================

def sync_data(scope: str, project: str = None, user: str = None,
              host: str = None, source: str = None, full: bool = False,
              agent: str = None) -> dict:
    """Execute granular sync."""
    global _sync_status

    if _sync_status["running"]:
        return {"error": "Sync already in progress", "status": _sync_status}

    with _sync_lock:
        _sync_status["running"] = True
        _sync_status["scope"] = scope
        _sync_status["started"] = datetime.now().isoformat()
        _sync_status["progress"] = "starting..."

        try:
            conn = get_write_connection()
            results = []

            # Snapshot existing conversation IDs before sync
            _pre_sync_ids = set(
                (r[0], r[1]) for r in conn.execute(
                    "SELECT session_id, project_id FROM conversations"
                ).fetchall()
            )

            # Pull from remote agents first (if configured)
            agents = _runtime_config.get("agents", [])
            pull_results = {}
            if agents and scope in ("all", "source", "agent"):
                agent_label = f" (agent: {agent})" if agent else ""
                _sync_status["progress"] = f"Pulling from remote agents{agent_label}..."
                pull_results = pull_from_agents(conn, agents, agent_filter=agent, full=full)

            if scope == "all":
                _sync_status["progress"] = "Parsing all projects..."
                claude_roots = get_claude_projects_dirs(include_desktop_hint=False)
                for root in claude_roots:
                    for project_dir in sorted(root.iterdir()):
                        if project_dir.is_dir():
                            _sync_status["progress"] = f"Parsing {extract_project_name(project_dir)}..."
                            stats = parse_project(project_dir, conn, full)
                            results.append(stats)

                _sync_status["progress"] = "Parsing Codex sessions..."
                codex_stats = parse_codex_sessions(conn, full)
                results.extend(codex_stats)

                _sync_status["progress"] = "Parsing Cowork sessions..."
                cowork_stats = parse_cowork_sessions(conn, full)
                results.extend(cowork_stats)

                try:
                    chatgpt_path = get_chatgpt_export_path()
                    if chatgpt_path:
                        _sync_status["progress"] = "Parsing ChatGPT export..."
                        chatgpt_stats = parse_chatgpt_export(chatgpt_path, conn, full=full)
                        results.append(chatgpt_stats)
                except Exception as e:
                    print(f"[sync] ChatGPT parse failed (non-fatal): {e}")

            elif scope == "project" and project:
                _sync_status["progress"] = f"Parsing project {project}..."
                claude_roots = get_claude_projects_dirs(include_desktop_hint=False)
                found = False
                for root in claude_roots:
                    for project_dir in root.iterdir():
                        if project_dir.is_dir() and extract_project_name(project_dir) == project:
                            stats = parse_project(project_dir, conn, full)
                            results.append(stats)
                            found = True
                            break
                    if found:
                        break
                if not found:
                    return {"error": f"Project '{project}' not found"}

            elif scope == "agent":
                # Pull-only: no local parse, just fetch from remote agent(s)
                # pull_from_agents already called above
                pass

            elif scope == "source" and source:
                _sync_status["progress"] = f"Parsing source {source}..."
                if source == SOURCE_CLAUDE_CODE:
                    claude_roots = get_claude_projects_dirs(include_desktop_hint=False)
                    for root in claude_roots:
                        for project_dir in sorted(root.iterdir()):
                            if project_dir.is_dir():
                                stats = parse_project(project_dir, conn, full)
                                results.append(stats)
                elif source == SOURCE_CODEX_CLI:
                    codex_stats = parse_codex_sessions(conn, full)
                    results.extend(codex_stats)
                elif source == SOURCE_COWORK_DESKTOP:
                    cowork_stats = parse_cowork_sessions(conn, full)
                    results.extend(cowork_stats)
                elif source == SOURCE_CHATGPT:
                    try:
                        chatgpt_path = get_chatgpt_export_path()
                        if chatgpt_path:
                            chatgpt_stats = parse_chatgpt_export(chatgpt_path, conn, full=full)
                            results.append(chatgpt_stats)
                    except Exception as e:
                        print(f"[sync] ChatGPT parse failed (non-fatal): {e}")

            # Find new conversations added during this sync
            _post_sync_ids = set(
                (r[0], r[1]) for r in conn.execute(
                    "SELECT session_id, project_id FROM conversations"
                ).fetchall()
            )
            new_conv_ids = _post_sync_ids - _pre_sync_ids
            conn.close()

            # Auto-classify only newly synced conversations
            classify_count = _auto_classify_new(conv_ids=new_conv_ids) if new_conv_ids else 0

            _sync_status["running"] = False
            _sync_status["progress"] = "completed"
            _sync_status["last_completed"] = datetime.now().isoformat()

            response = {
                "success": True,
                "scope": scope,
                "results": results,
            }
            if pull_results:
                response["pull"] = pull_results
            if classify_count:
                response["classified"] = classify_count
            return response

        except Exception as e:
            _sync_status["running"] = False
            _sync_status["progress"] = f"error: {str(e)}"
            return {"error": str(e)}


# ===========================================================================
# THREADING HTTP SERVER
# ===========================================================================

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Thread-per-request HTTP server."""
    allow_reuse_address = True
    daemon_threads = True


class APIHandler(SimpleHTTPRequestHandler):
    """HTTP handler for API endpoints and static files."""

    # Endpoints allowed in agent-only mode
    _AGENT_PATHS = {"/api/health", "/api/info", "/api/export"}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        # Role gating: agent mode only serves thin endpoints
        if _runtime_config["role"] == "agent" and path.startswith("/api/") and path not in self._AGENT_PATHS:
            self.send_error(404, f"Endpoint not available in agent mode")
            return

        # ==== DIMENSION LISTINGS ====

        if path == "/api/projects":
            conn = get_read_connection()
            if not conn:
                self.send_json({"projects": [], "note": "No database yet"})
                return
            try:
                ids = resolve_ids(conn, params)
                data = get_projects_list(conn, user_id=ids["user_id"], host_id=ids["host_id"])
                self.send_json(data)
            finally:
                conn.close()
            return

        if path == "/api/users":
            conn = get_read_connection()
            if not conn:
                self.send_json([])
                return
            try:
                data = get_users_list(conn)
                self.send_json(data)
            finally:
                conn.close()
            return

        if path.startswith("/api/user/"):
            username = path.split("/api/user/")[1]
            conn = get_read_connection()
            if not conn:
                self.send_error(404, "No database")
                return
            try:
                data = get_user_detail(conn, username)
                if not data:
                    self.send_error(404, f"User '{username}' not found")
                    return
                self.send_json(data)
            finally:
                conn.close()
            return

        if path == "/api/hosts":
            conn = get_read_connection()
            if not conn:
                self.send_json([])
                return
            try:
                data = get_hosts_list(conn)
                self.send_json(data)
            finally:
                conn.close()
            return

        # ==== MAIN DATA ENDPOINT ====

        if path == "/api/data":
            conn = get_read_connection()
            if not conn:
                self.send_error(404, "No database yet. Run parser first.")
                return

            try:
                ids = resolve_ids(conn, params)
                start_date = params.get("start", [None])[0]
                end_date = params.get("end", [None])[0]
                client_source = params.get("client", [None])[0]

                filter_kwargs = {
                    "project_id": ids["project_id"],
                    "user_id": ids["user_id"],
                    "host_id": ids["host_id"],
                    "start_date": start_date,
                    "end_date": end_date,
                    "client_source": client_source,
                }

                tokens_data = get_token_stats(conn, **filter_kwargs)
                files_data = get_files_stats(conn, **filter_kwargs)
                bash_data = get_bash_stats(conn, **filter_kwargs)
                searches_data = get_searches_stats(conn, **filter_kwargs)
                skills_data = get_skills_stats(conn, **filter_kwargs)
                subagents_data = get_subagents_stats(conn, **filter_kwargs)
                mcp_data = get_mcp_stats(conn, **filter_kwargs)

                # Top-level summaries for frontend overview cards
                token_totals = tokens_data.get("totals", {})
                total_tokens = ((token_totals.get("input_tokens") or 0) +
                                (token_totals.get("output_tokens") or 0) +
                                (token_totals.get("cache_creation") or 0) +
                                (token_totals.get("cache_read") or 0))

                # Conversation + message counts (with client/source filter)
                where_c, params_c = build_filters(ids["project_id"], ids["user_id"], ids["host_id"],
                                                   start_date, end_date, client_source, 'conversations')
                ss_join_c = " LEFT JOIN session_sources ss ON ss.session_id = conversations.session_id AND ss.project_id = conversations.project_id" if client_source else ""
                conv_row = conn.execute("SELECT COUNT(*) FROM conversations" + ss_join_c + where_c, params_c).fetchone()

                where_m, params_m = build_filters(ids["project_id"], ids["user_id"], ids["host_id"],
                                                   start_date, end_date, client_source, 'messages')
                ss_join_m = " LEFT JOIN session_sources ss ON ss.session_id = messages.session_id AND ss.project_id = messages.project_id" if client_source else ""
                msg_row = conn.execute("SELECT COUNT(*) FROM messages" + ss_join_m + where_m, params_m).fetchone()

                # By-project, by-user, by-model breakdowns (with client/source filter)
                where_bp, params_bp = build_filters(ids["project_id"], ids["user_id"], ids["host_id"],
                                                     start_date, end_date, client_source, 'c')
                ss_join = "LEFT JOIN session_sources ss ON ss.session_id = c.session_id AND ss.project_id = c.project_id" if client_source else ""

                by_project = conn.execute(
                    f"""SELECT p.name, COUNT(DISTINCT c.session_id) as sessions,
                              SUM(c.total_tokens) as tokens
                       FROM conversations c
                       JOIN projects p ON c.project_id = p.id
                       {ss_join}""" +
                    where_bp + " GROUP BY p.name ORDER BY sessions DESC", params_bp
                ).fetchall()

                by_user = conn.execute(
                    f"""SELECT u.username, COUNT(DISTINCT c.session_id) as sessions,
                              SUM(c.total_tokens) as tokens
                       FROM conversations c
                       JOIN users u ON c.user_id = u.id
                       {ss_join}""" +
                    where_bp + " GROUP BY u.username ORDER BY sessions DESC", params_bp
                ).fetchall()

                by_model = conn.execute(
                    f"""SELECT COALESCE(NULLIF(c.model, ''), 'unknown') as model,
                              COUNT(DISTINCT c.session_id) as sessions
                       FROM conversations c
                       {ss_join}""" +
                    where_bp + " GROUP BY model ORDER BY sessions DESC", params_bp
                ).fetchall()

                # Flatten tokens for frontend compatibility
                tokens_flat = {
                    "input": token_totals.get("input_tokens", 0),
                    "output": token_totals.get("output_tokens", 0),
                    "cache_creation": token_totals.get("cache_creation", 0),
                    "cache_read": token_totals.get("cache_read", 0),
                    "total": total_tokens,
                    "by_model": tokens_data.get("by_model", []),
                    "daily": tokens_data.get("daily", []),
                    "by_source": tokens_data.get("by_source", []),
                    "qa": tokens_data.get("qa", {}),
                    "totals": token_totals,
                }

                data = {
                    "filters": {
                        "project_id": ids["project_id"],
                        "user_id": ids["user_id"],
                        "host_id": ids["host_id"],
                        "start": start_date,
                        "end": end_date,
                        "client": client_source,
                    },
                    # Top-level summaries
                    "total_conversations": conv_row[0] if conv_row else 0,
                    "total_tokens": total_tokens,
                    "total_messages": msg_row[0] if msg_row else 0,
                    "total_file_ops": files_data.get("totals", {}).get("total_ops", 0),
                    "total_bash": bash_data.get("totals", {}).get("total_commands", 0),
                    "by_project": [{"name": r[0], "sessions": r[1], "tokens": r[2] or 0} for r in by_project],
                    "by_user": [{"username": r[0], "sessions": r[1], "tokens": r[2] or 0} for r in by_user],
                    "by_model": [{"model": r[0], "sessions": r[1]} for r in by_model],
                    # Nested data
                    "date_range": get_date_range(conn, **{k: v for k, v in filter_kwargs.items() if k != "start_date" and k != "end_date"}),
                    "tokens": tokens_flat,
                    "skills": skills_data,
                    "subagents": subagents_data,
                    "mcp": mcp_data,
                    "timeline": get_timeline_stats(conn, **filter_kwargs),
                    "files": files_data,
                    "bash": bash_data,
                    "searches": searches_data,
                    "clients": get_client_stats(conn, **filter_kwargs),
                }
                self.send_json(data)
            finally:
                conn.close()
            return

        # ==== CONVERSATIONS ====

        if path == "/api/conversations":
            conn = get_read_connection()
            if not conn:
                self.send_error(404, "No database")
                return
            try:
                ids = resolve_ids(conn, params)
                search = params.get("search", [None])[0]
                start_date = params.get("start", [None])[0]
                end_date = params.get("end", [None])[0]
                client_source = params.get("client", [None])[0]
                classification = params.get("classification", [None])[0]
                sensitivity = params.get("sensitivity", [None])[0]
                limit = int(params.get("limit", [50])[0])
                offset = int(params.get("offset", [0])[0])

                data = get_conversations_list(
                    conn,
                    project_id=ids["project_id"],
                    user_id=ids["user_id"],
                    host_id=ids["host_id"],
                    search=search,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                    offset=offset,
                    client_source=client_source,
                    classification=classification,
                    sensitivity=sensitivity,
                )
                self.send_json(data)
            finally:
                conn.close()
            return

        if path.startswith("/api/conversation/") and path.endswith("/metadata"):
            session_id = unquote(path.split("/api/conversation/")[1].split("/")[0])
            conn = get_read_connection()
            if not conn:
                self.send_error(404, "No database")
                return
            try:
                ids = resolve_ids(conn, params)
                data = get_conversation_metadata(conn, session_id, project_id=ids.get("project_id"))
                if not data:
                    self.send_json({"classified": False, "session_id": session_id})
                    return
                self.send_json({"classified": True, "metadata": data})
            finally:
                conn.close()
            return

        if path.startswith("/api/conversation/"):
            session_id = unquote(path.split("/api/conversation/")[1].split("/")[0])
            conn = get_read_connection()
            if not conn:
                self.send_error(404, "No database")
                return
            try:
                ids = resolve_ids(conn, params)
                data = get_conversation_detail(conn, session_id, project_id=ids["project_id"])
                if not data:
                    self.send_error(404, f"Conversation '{session_id}' not found")
                    return
                self.send_json(data)
            finally:
                conn.close()
            return

        # ==== CLASSIFICATION STATS ====

        if path == "/api/classifications/tagcloud":
            conn = get_read_connection()
            if not conn:
                self.send_json({"error": "No database"})
                return
            try:
                ids = resolve_ids(conn, params)
                start_date = params.get("start", [None])[0]
                end_date = params.get("end", [None])[0]
                client_source = params.get("client", [None])[0]
                data = get_tagcloud_data(
                    conn,
                    project_id=ids["project_id"],
                    user_id=ids["user_id"],
                    host_id=ids["host_id"],
                    start_date=start_date,
                    end_date=end_date,
                    client_source=client_source,
                )
                self.send_json(data)
            finally:
                conn.close()
            return

        if path == "/api/classifications/stats":
            conn = get_read_connection()
            if not conn:
                self.send_json({"error": "No database"})
                return
            try:
                ids = resolve_ids(conn, params)
                start_date = params.get("start", [None])[0]
                end_date = params.get("end", [None])[0]
                client_source = params.get("client", [None])[0]

                data = get_classification_stats(
                    conn,
                    project_id=ids["project_id"],
                    user_id=ids["user_id"],
                    host_id=ids["host_id"],
                    start_date=start_date,
                    end_date=end_date,
                    client_source=client_source,
                )
                self.send_json(data)
            finally:
                conn.close()
            return

        # ==== AGENT ENDPOINTS (health, info, export) ====

        if path == "/api/health":
            import platform as _platform
            uptime = (datetime.now() - _server_start_time).total_seconds()
            self.send_json({
                "status": "ok",
                "hostname": socket.gethostname(),
                "role": _runtime_config["role"],
                "uptime": round(uptime, 1),
                "version": 1,
            })
            return

        if path == "/api/info":
            import platform as _platform
            conn = get_read_connection()
            info = {
                "hostname": socket.gethostname(),
                "os": _platform.system(),
                "role": _runtime_config["role"],
            }
            if conn:
                try:
                    sources = run_query(conn, "SELECT DISTINCT source FROM session_sources")
                    sessions_count = run_query(conn, "SELECT COUNT(*) as c FROM conversations")[0]["c"]
                    last_parse = run_query(conn, "SELECT MAX(value) as v FROM parse_state WHERE key LIKE 'last_%'")
                    info["sources"] = [s["source"] for s in sources]
                    info["sessions_count"] = sessions_count
                    info["last_parse"] = last_parse[0]["v"] if last_parse else None
                finally:
                    conn.close()
            if UNIFIED_DB_PATH.exists():
                info["db_size_bytes"] = UNIFIED_DB_PATH.stat().st_size
            agents = _runtime_config.get("agents", [])
            info["agents"] = [a["name"] for a in agents]
            self.send_json(info)
            return

        if path == "/api/export":
            since = params.get("since", ["1970-01-01T00:00:00"])[0]
            limit = int(params.get("limit", [1000])[0])
            conn = get_read_connection()
            if not conn:
                self.send_json({
                    "schema_version": 1,
                    "host": {"hostname": socket.gethostname()},
                    "user": {},
                    "exported_at": datetime.now().isoformat(),
                    "sessions": [],
                })
                return
            try:
                import platform as _platform
                sessions = export_sessions(conn, since, limit)
                package = {
                    "schema_version": 1,
                    "host": {
                        "hostname": socket.gethostname(),
                        "os_type": _platform.system(),
                        "home_dir": str(Path.home()),
                    },
                    "user": {
                        "username": getpass.getuser(),
                    },
                    "exported_at": datetime.now().isoformat(),
                    "sessions": sessions,
                }
                self.send_json(package)
            finally:
                conn.close()
            return

        # ==== SYNC STATUS ====

        if path == "/api/sync/status":
            self.send_json(_sync_status)
            return

        # ==== SEARCH ====

        if path == "/api/search":
            query = params.get("q", [None])[0]
            if not query:
                self.send_error(400, "q (query) required")
                return

            conn = get_read_connection()
            if not conn:
                self.send_error(404, "No database")
                return
            try:
                ids = resolve_ids(conn, params)
                limit = int(params.get("limit", [20])[0])
                client_source = params.get("client", [None])[0]
                classification = params.get("classification", [None])[0]
                sensitivity = params.get("sensitivity", [None])[0]
                topic = params.get("topic", [None])[0]
                results = search_messages(
                    conn, query, limit,
                    project_id=ids["project_id"],
                    user_id=ids["user_id"],
                    host_id=ids["host_id"],
                    client_source=client_source,
                    classification=classification,
                    sensitivity=sensitivity,
                    topic=topic,
                )
                self.send_json({
                    "query": query,
                    "results": results,
                    "total": len(results),
                })
            except Exception as e:
                self.send_error(500, f"Search error: {str(e)}")
            finally:
                conn.close()
            return

        # ==== KB ENDPOINTS ====

        if path == "/api/kb/status":
            session_id = params.get("session_id", [None])[0]
            if not session_id:
                self.send_error(400, "session_id required")
                return
            try:
                store = get_kb_store()
                payload = store.get(session_id)
                self.send_json({
                    "indexed": bool(payload),
                    "payload": payload,
                    "session_id": session_id,
                })
            except Exception as e:
                self.send_error(500, f"KB status error: {str(e)}")
            return

        if path == "/api/kb/search":
            query = params.get("q", [None])[0]
            if not query:
                self.send_error(400, "q (query) required")
                return
            limit = int(params.get("limit", [10])[0])
            filters = {}
            if "classification" in params:
                filters["classification"] = params["classification"][0]
            if "tags" in params:
                filters["tags"] = params["tags"][0].split(",")
            if "project" in params:
                filters["project"] = params["project"][0]
            if "user" in params:
                filters["user"] = params["user"][0]
            try:
                store = get_kb_store()
                results = store.search(query, limit=limit, filters=filters if filters else None)
                self.send_json({
                    "query": query,
                    "results": [
                        {"session_id": r.session_id, "score": r.score, "payload": r.payload}
                        for r in results
                    ],
                    "total": len(results),
                })
            except Exception as e:
                self.send_error(500, f"KB search error: {str(e)}")
            return

        if path == "/api/kb/tags":
            try:
                store = get_kb_store(require_openai=False)
                if store is None:
                    self.send_json({"tags": {}, "note": "KB not initialized"})
                    return
                tag_counts = store.list_all_tags()
                self.send_json({"tags": tag_counts})
            except Exception as e:
                self.send_error(500, f"KB tags error: {str(e)}")
            return

        if path == "/api/kb/count":
            try:
                store = get_kb_store(require_openai=False)
                if store is None:
                    self.send_json({"count": 0, "note": "KB not initialized"})
                    return
                self.send_json({"count": store.count()})
            except Exception as e:
                self.send_error(500, f"KB count error: {str(e)}")
            return

        # ==== RETENTION / SETTINGS ====

        if path == "/api/retention/status":
            conn = get_read_connection()
            if not conn:
                self.send_json({"error": "No database"})
                return
            try:
                db_size = UNIFIED_DB_PATH.stat().st_size if UNIFIED_DB_PATH.exists() else 0
                tables = run_query(conn, """
                    SELECT name FROM sqlite_master WHERE type='table'
                    AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%fts%'
                """)
                table_counts = {}
                for t in tables:
                    try:
                        count = run_query(conn, f"SELECT COUNT(*) as c FROM [{t['name']}]")[0]['c']
                        table_counts[t['name']] = count
                    except:
                        table_counts[t['name']] = -1

                self.send_json({
                    "db_path": str(UNIFIED_DB_PATH),
                    "db_size_bytes": db_size,
                    "db_size_mb": round(db_size / (1024 * 1024), 2),
                    "tables": table_counts,
                })
            finally:
                conn.close()
            return

        # ==== STATIC FILES ====
        if path == "/" or path == "":
            self.path = "/dashboard.html"

        return super().do_GET()

    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        # Role gating: agent mode has no POST endpoints
        if _runtime_config["role"] == "agent":
            self.send_error(404, "Endpoint not available in agent mode")
            return

        content_length = int(self.headers.get('Content-Length', 0))
        body = {}
        if content_length > 0:
            raw_body = self.rfile.read(content_length)
            try:
                body = json.loads(raw_body.decode('utf-8'))
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON body")
                return

        # ==== SYNC ====

        if path == "/api/sync":
            scope = body.get("scope", "all")
            project = body.get("project")
            user = body.get("user")
            host = body.get("host")
            source = body.get("source")
            agent = body.get("agent")
            full = body.get("full_reparse", False)

            # Run sync in background thread
            def do_sync():
                sync_data(scope, project=project, user=user, host=host, source=source, full=full, agent=agent)

            thread = threading.Thread(target=do_sync, daemon=True)
            thread.start()

            self.send_json({
                "started": True,
                "scope": scope,
                "message": "Sync started in background. Check /api/sync/status for progress.",
            })
            return

        # ==== KB INDEX ====

        if path == "/api/kb/index":
            session_id = body.get("session_id")
            project = body.get("project")
            user = body.get("user")
            tags = body.get("tags", [])
            pre_metadata = body.get("metadata")

            if not session_id:
                self.send_error(400, "session_id required")
                return

            try:
                conn = get_read_connection()
                if not conn:
                    self.send_error(404, "No database")
                    return

                conv_data = get_conversation_detail(conn, session_id)
                conn.close()

                if not conv_data:
                    self.send_error(404, f"Conversation '{session_id}' not found")
                    return

                indexer = get_kb_indexer()
                payload = indexer.index(
                    session_id=session_id,
                    messages=conv_data["messages"],
                    project=project or conv_data["conversation"].get("project", ""),
                    timestamp=conv_data["conversation"].get("timestamp", ""),
                    user=user or conv_data["conversation"].get("username", ""),
                    custom_tags=tags,
                    pre_metadata=pre_metadata,
                )

                self.send_json({
                    "success": True,
                    "session_id": session_id,
                    "payload": payload,
                })
            except Exception as e:
                self.send_error(500, f"KB index error: {str(e)}")
            return

        self.send_error(404, "Not found")

    def do_DELETE(self):
        """Handle DELETE requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/kb/index":
            session_id = params.get("session_id", [None])[0]
            if not session_id:
                self.send_error(400, "session_id required")
                return
            try:
                store = get_kb_store()
                store.delete(session_id)
                self.send_json({"success": True, "session_id": session_id})
            except Exception as e:
                self.send_error(500, f"KB delete error: {str(e)}")
            return

        self.send_error(404, "Not found")

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def send_json(self, data):
        """Send JSON response."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        """Custom log format."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


def main():
    import os
    os.chdir(Path(__file__).parent)

    role = _runtime_config["role"]
    port = _runtime_config.get("port", PORT)
    # Agent and both: bind 0.0.0.0 for remote access; collector-only: localhost
    bind_addr = "localhost" if role == "collector" else "0.0.0.0"

    server = ThreadingHTTPServer((bind_addr, port), APIHandler)
    print(f"LocalAgentViewer Server")
    print(f"  Role: {role}")
    print(f"  Bind: {bind_addr}:{port}")
    if role != "agent":
        print(f"  Dashboard: http://localhost:{port}")
        print(f"  API: http://localhost:{port}/api/data")
    else:
        print(f"  Export: http://0.0.0.0:{port}/api/export")
    print(f"  Database: {UNIFIED_DB_PATH}")
    agents = _runtime_config.get("agents", [])
    if agents:
        print(f"  Remote agents: {', '.join(a['name'] for a in agents)}")
    print(f"  Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")


if __name__ == "__main__":
    main()

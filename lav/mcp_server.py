#!/usr/bin/env python3
"""
LocalAgentViewer MCP Server

FastMCP server exposing interaction search (SQL + Qdrant semantic),
knowledge base indexing, and sync trigger.

Tools (read):
  - get_interactions: list/search interactions (FTS SQLite)
  - get_interaction_details: full transcript by session_id
  - semantic_search: Qdrant vector search on indexed KB
  - kb_status: check if interaction is indexed

Tools (write, require LAV_API_KEY):
  - kb_index: index interaction into Qdrant (auto-tag via Haiku or pre-metadata)
  - kb_remove: remove interaction from Qdrant
  - kb_update_tags: update tags without re-embedding
  - sync: trigger data re-parse
"""

import os
import sqlite3
from pathlib import Path
from typing import Optional

import lav  # noqa: F401 — triggers .env loading
from fastmcp import FastMCP
from lav.config import UNIFIED_DB_PATH, QDRANT_DATA_DIR, QDRANT_COLLECTION, QDRANT_URL

# Lazy imports to avoid loading heavy deps at startup
_kb_store = None


def _get_read_connection() -> Optional[sqlite3.Connection]:
    """Read-only connection to the unified DB."""
    if not UNIFIED_DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _get_kb_store():
    """Lazy-init Qdrant vector store (HTTP or file mode)."""
    global _kb_store
    if _kb_store is None:
        from lav.qdrant.store import InteractionVectorStore
        if QDRANT_URL:
            _kb_store = InteractionVectorStore(url=QDRANT_URL, collection=QDRANT_COLLECTION)
        else:
            QDRANT_DATA_DIR.mkdir(parents=True, exist_ok=True)
            _kb_store = InteractionVectorStore(data_path=QDRANT_DATA_DIR, collection=QDRANT_COLLECTION)
        _kb_store.ensure_collection()
    return _kb_store


def _check_api_key(api_key: str) -> bool:
    """Validate api_key against LAV_API_KEY env var (write operations)."""
    expected = os.environ.get("LAV_API_KEY", "")
    if not expected:
        return False
    return api_key == expected


def _check_read_api_key(api_key: Optional[str] = None) -> bool:
    """Validate api_key against LAV_READ_API_KEY env var (read operations).

    If LAV_READ_API_KEY is not set, read access is open (backwards compatible).
    """
    expected = os.environ.get("LAV_READ_API_KEY", "")
    if not expected:
        return True
    return api_key == expected


# ── MCP Server ──────────────────────────────────────────────

mcp = FastMCP(
    "local-agent-viewer",
    instructions=(
        "Search and explore Claude Code interaction history. "
        "Supports SQL full-text search, Qdrant semantic search, "
        "and data sync triggers."
    ),
)


@mcp.tool()
def get_interactions(
    search: Optional[str] = None,
    project: Optional[str] = None,
    user: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    limit: int = 20,
    api_key: Optional[str] = None,
) -> dict:
    """List interactions with optional filters.

    Args:
        search: Full-text search in message content
        project: Filter by project name
        user: Filter by username
        start: Start date (YYYY-MM-DD)
        end: End date (YYYY-MM-DD)
        limit: Max results (default 20)
        api_key: LAV_READ_API_KEY (required if set on server)
    """
    if not _check_read_api_key(api_key):
        return {"error": "Invalid or missing api_key (LAV_READ_API_KEY)"}

    from lav.queries import get_interactions_list, run_query

    conn = _get_read_connection()
    if not conn:
        return {"error": "No database. Run sync first."}

    try:
        # Resolve names to IDs
        project_id = None
        user_id = None
        if project:
            row = run_query(conn, "SELECT id FROM projects WHERE name = ?", [project])
            if row:
                project_id = row[0]["id"]
        if user:
            row = run_query(conn, "SELECT id FROM users WHERE username = ?", [user])
            if row:
                user_id = row[0]["id"]

        data = get_interactions_list(
            conn,
            project_id=project_id,
            user_id=user_id,
            search=search,
            start_date=start,
            end_date=end,
            limit=limit,
        )
        return data
    finally:
        conn.close()


@mcp.tool()
def get_interaction_details(session_id: str, api_key: Optional[str] = None) -> dict:
    """Get full interaction transcript by session ID.

    Args:
        session_id: The interaction session UUID
        api_key: LAV_READ_API_KEY (required if set on server)
    """
    if not _check_read_api_key(api_key):
        return {"error": "Invalid or missing api_key (LAV_READ_API_KEY)"}

    from lav.queries import get_interaction_detail

    conn = _get_read_connection()
    if not conn:
        return {"error": "No database. Run sync first."}

    try:
        data = get_interaction_detail(conn, session_id)
        if not data:
            return {"error": f"Interaction '{session_id}' not found"}
        return data
    finally:
        conn.close()


@mcp.tool()
def semantic_search(
    query: str,
    limit: int = 10,
    classification: Optional[str] = None,
    tags: Optional[str] = None,
    project: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    """Semantic search in the indexed knowledge base (Qdrant).

    Args:
        query: Natural language search query
        limit: Max results (default 10)
        classification: Filter by type (development, meeting, analysis, brainstorm, support, learning)
        tags: Comma-separated tags to filter by
        project: Filter by project name
        api_key: LAV_READ_API_KEY (required if set on server)
    """
    if not _check_read_api_key(api_key):
        return {"error": "Invalid or missing api_key (LAV_READ_API_KEY)"}

    try:
        store = _get_kb_store()
    except Exception as e:
        return {"error": f"KB not available: {e}"}

    filters = {}
    if classification:
        filters["classification"] = classification
    if tags:
        filters["tags"] = [t.strip() for t in tags.split(",")]
    if project:
        filters["project"] = project

    results = store.search(query, limit=limit, filters=filters if filters else None)
    return {
        "query": query,
        "results": [
            {"session_id": r.session_id, "score": r.score, "payload": r.payload}
            for r in results
        ],
        "total": len(results),
    }


@mcp.tool()
def sync(
    api_key: str,
    scope: str = "all",
    project: Optional[str] = None,
    source: Optional[str] = None,
    full_reparse: bool = False,
) -> dict:
    """Trigger data sync/re-parse. Requires API key.

    Args:
        api_key: LAV_API_KEY for authorization
        scope: Sync scope - "all", "project", or "source"
        project: Project name (required when scope="project")
        source: Source type (required when scope="source"): claude_code, codex_cli, cowork_desktop
        full_reparse: If true, re-parse all files (not just new ones)
    """
    if not _check_api_key(api_key):
        return {"error": "Invalid or missing api_key"}

    # Import sync_data from server module
    from lav.server import sync_data

    result = sync_data(
        scope=scope,
        project=project,
        source=source,
        full=full_reparse,
    )
    return result


@mcp.tool()
def kb_index(
    api_key: str,
    session_id: str,
    tags: Optional[str] = None,
    pre_metadata: Optional[str] = None,
) -> dict:
    """Index an interaction into the semantic knowledge base (Qdrant).

    Retrieves the interaction from SQLite, generates metadata via Haiku
    (or uses pre-computed metadata), embeds the content, and stores in Qdrant.

    Args:
        api_key: LAV_API_KEY for authorization
        session_id: The interaction session UUID to index
        tags: Optional comma-separated tags (e.g. "importante,cliente-coop")
        pre_metadata: Optional JSON string with pre-computed metadata
            (keys: summary, classification, topics, people, clients).
            If provided, skips Haiku auto-tagging.
    """
    if not _check_api_key(api_key):
        return {"error": "Invalid or missing api_key"}

    try:
        store = _get_kb_store()
    except Exception as e:
        return {"error": f"KB not available: {e}"}

    # Get interaction from SQLite
    from lav.queries import get_interaction_detail
    conn = _get_read_connection()
    if not conn:
        return {"error": "No database. Run sync first."}

    try:
        data = get_interaction_detail(conn, session_id)
    finally:
        conn.close()

    if not data:
        return {"error": f"Interaction '{session_id}' not found in SQLite"}

    conv = data["interaction"]
    messages = data["messages"]

    # Parse optional inputs
    import json
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    metadata = json.loads(pre_metadata) if pre_metadata else None

    # Index via InteractionIndexer
    from lav.qdrant.indexer import InteractionIndexer
    indexer = InteractionIndexer(store)

    try:
        payload = indexer.index(
            session_id=session_id,
            messages=messages,
            project=conv.get("project_name", ""),
            timestamp=conv.get("timestamp", ""),
            user=conv.get("username", ""),
            custom_tags=tag_list if tag_list else None,
            pre_metadata=metadata,
        )
    except Exception as e:
        return {"error": f"Indexing failed: {e}"}

    return {
        "status": "indexed",
        "session_id": session_id,
        "payload": payload,
    }


@mcp.tool()
def kb_remove(
    api_key: str,
    session_id: str,
) -> dict:
    """Remove an interaction from the semantic knowledge base (Qdrant).

    Args:
        api_key: LAV_API_KEY for authorization
        session_id: The interaction session UUID to remove
    """
    if not _check_api_key(api_key):
        return {"error": "Invalid or missing api_key"}

    try:
        store = _get_kb_store()
    except Exception as e:
        return {"error": f"KB not available: {e}"}

    store.delete(session_id)
    return {"status": "removed", "session_id": session_id}


@mcp.tool()
def kb_status(
    session_id: str,
    api_key: Optional[str] = None,
) -> dict:
    """Check if an interaction is indexed in the knowledge base.

    Args:
        session_id: The interaction session UUID to check
        api_key: LAV_READ_API_KEY (required if set on server)
    """
    if not _check_read_api_key(api_key):
        return {"error": "Invalid or missing api_key (LAV_READ_API_KEY)"}

    try:
        store = _get_kb_store()
    except Exception as e:
        return {"error": f"KB not available: {e}"}

    indexed = store.is_indexed(session_id)
    payload = store.get(session_id) if indexed else None

    return {
        "session_id": session_id,
        "indexed": indexed,
        "payload": payload,
    }


@mcp.tool()
def kb_update_tags(
    api_key: str,
    session_id: str,
    tags: str,
) -> dict:
    """Update tags on an indexed interaction without re-embedding.

    Args:
        api_key: LAV_API_KEY for authorization
        session_id: The interaction session UUID
        tags: Comma-separated tags (replaces existing tags)
    """
    if not _check_api_key(api_key):
        return {"error": "Invalid or missing api_key"}

    try:
        store = _get_kb_store()
    except Exception as e:
        return {"error": f"KB not available: {e}"}

    if not store.is_indexed(session_id):
        return {"error": f"Interaction '{session_id}' not indexed. Use kb_index first."}

    tag_list = [t.strip() for t in tags.split(",")]
    store.update_tags(session_id, tag_list)

    return {
        "status": "updated",
        "session_id": session_id,
        "tags": tag_list,
    }


@mcp.tool()
def manage_pricing(
    action: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    input_price_per_mtok: Optional[float] = None,
    output_price_per_mtok: Optional[float] = None,
    cache_write_price_per_mtok: float = 0,
    cache_read_price_per_mtok: float = 0,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Manage model pricing data for cost tracking.

    Args:
        action: "list" (read), "add" (write), or "lookup" (returns prompt for web search)
        api_key: LAV_READ_API_KEY for list/lookup, LAV_API_KEY for add
        model: Model name (required for add and lookup)
        provider: Provider name (anthropic, openai, etc.)
        input_price_per_mtok: Input price per 1M tokens (required for add)
        output_price_per_mtok: Output price per 1M tokens (required for add)
        cache_write_price_per_mtok: Cache write price per 1M tokens (default 0)
        cache_read_price_per_mtok: Cache read price per 1M tokens (default 0)
        from_date: Start date YYYY-MM-DD (required for add)
        to_date: End date YYYY-MM-DD, exclusive (optional, NULL = current)
        notes: Optional notes
    """
    if action == "list":
        if not _check_read_api_key(api_key):
            return {"error": "Invalid or missing api_key (LAV_READ_API_KEY)"}
        conn = _get_read_connection()
        if not conn:
            return {"error": "No database. Run sync first."}
        try:
            from lav.pricing import get_pricing
            return {"pricing": get_pricing(conn, model=model)}
        finally:
            conn.close()

    elif action == "add":
        if not _check_api_key(api_key):
            return {"error": "Invalid or missing api_key"}
        if not model or input_price_per_mtok is None or output_price_per_mtok is None or not from_date:
            return {"error": "model, input_price_per_mtok, output_price_per_mtok, and from_date are required"}
        conn = sqlite3.connect(str(UNIFIED_DB_PATH))
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            from lav.pricing import upsert_pricing
            upsert_pricing(
                conn, model=model, input_price=input_price_per_mtok,
                output_price=output_price_per_mtok, from_date=from_date,
                provider=provider, cache_write=cache_write_price_per_mtok,
                cache_read=cache_read_price_per_mtok, to_date=to_date,
                notes=notes,
            )
            return {"status": "added", "model": model, "from_date": from_date}
        finally:
            conn.close()

    elif action == "lookup":
        if not _check_read_api_key(api_key):
            return {"error": "Invalid or missing api_key (LAV_READ_API_KEY)"}
        if not model:
            return {"error": "model is required for lookup"}
        provider_str = f" ({provider})" if provider else ""
        prompt = (
            f'Search the web for the current API pricing of model "{model}"{provider_str}.\n'
            f"Find the official pricing page for the provider.\n"
            f"Extract these values in USD per million tokens:\n"
            f"- Input token price\n"
            f"- Output token price\n"
            f"- Cached input price (if available, otherwise 0)\n"
            f"- Cache write price (if available, otherwise 0)\n\n"
            f"Return ONLY a JSON object:\n"
            f'{{"input_price_per_mtok": X, "output_price_per_mtok": X, '
            f'"cache_write_price_per_mtok": X, "cache_read_price_per_mtok": X}}\n\n'
            f"Provider pricing pages:\n"
            f"- Anthropic: https://docs.anthropic.com/en/docs/about-claude/pricing\n"
            f"- OpenAI: https://platform.openai.com/docs/pricing"
        )
        return {"lookup_prompt": prompt, "model": model, "provider": provider}

    else:
        return {"error": f"Unknown action '{action}'. Use 'list', 'add', or 'lookup'."}


def main():
    mcp.run()


if __name__ == "__main__":
    main()

"""
Predefined SQL queries for LocalAgentViewer statistics and analysis.

All queries support 4-dimensional filtering: project_id, user_id, host_id, source.
"""


def run_query(conn, query, params=None):
    """Execute a query and return results as list of dicts."""
    cursor = conn.cursor()
    if params:
        cursor.execute(query, params)
    else:
        cursor.execute(query)

    columns = [description[0] for description in cursor.description]
    results = []
    for row in cursor.fetchall():
        results.append(dict(zip(columns, row)))
    return results


def build_filters(project_id=None, user_id=None, host_id=None,
                  start=None, end=None, client=None, table_alias='t'):
    """Build WHERE clause for 4-dimensional filtering.

    Args:
        project_id: Filter by project (integer ID)
        user_id: Filter by user (integer ID)
        host_id: Filter by host (integer ID)
        start: Start date (YYYY-MM-DD string)
        end: End date (YYYY-MM-DD string)
        client: Source/client filter (claude_code, codex_cli, cowork_desktop)
        table_alias: SQL table alias for the main table

    Returns:
        Tuple of (where_clause_string, params_list)
    """
    clauses, params = [], []

    if project_id is not None:
        clauses.append(f"{table_alias}.project_id = ?")
        params.append(project_id)
    if user_id is not None:
        clauses.append(f"{table_alias}.user_id = ?")
        params.append(user_id)
    if host_id is not None:
        clauses.append(f"{table_alias}.host_id = ?")
        params.append(host_id)
    if start:
        clauses.append(f"{table_alias}.timestamp >= ?")
        params.append(start)
    if end:
        clauses.append(f"{table_alias}.timestamp <= ?")
        params.append(end + "T23:59:59")
    if client:
        clauses.append("ss.source = ?")
        params.append(client)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _join_session_sources(table_alias='t'):
    """Return LEFT JOIN clause for session_sources."""
    return f"LEFT JOIN session_sources ss ON ss.session_id = {table_alias}.session_id AND ss.project_id = {table_alias}.project_id"


# ===========================================================================
# TOKEN STATS
# ===========================================================================

def get_token_stats(conn, project_id=None, user_id=None, host_id=None,
                    start_date=None, end_date=None, client_source=None):
    """Get token usage statistics with 4D filters."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='tu'
    )
    join = _join_session_sources('tu')

    price_join = """LEFT JOIN model_pricing mp
        ON mp.model = tu.model
        AND tu.timestamp >= mp.from_date
        AND (mp.to_date IS NULL OR tu.timestamp < mp.to_date)"""

    by_model = run_query(conn, f"""
        SELECT
            tu.model as model,
            COUNT(*) as calls,
            SUM(tu.input_tokens) as input_tokens,
            SUM(tu.output_tokens) as output_tokens,
            SUM(tu.cache_creation_tokens) as cache_creation,
            SUM(tu.cache_read_tokens) as cache_read,
            ROUND(SUM(COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0), 4) as cost_input,
            ROUND(SUM(COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0), 4) as cost_output,
            ROUND(SUM(COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0), 4) as cost_cache_write,
            ROUND(SUM(COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0), 4) as cost_cache_read,
            ROUND(SUM(
                COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
            ), 4) as cost_usd
        FROM token_usage tu
        {join}
        {price_join}
        {where}
        GROUP BY tu.model
        ORDER BY calls DESC
    """, params if params else None)

    daily = run_query(conn, f"""
        SELECT
            DATE(tu.timestamp) as date,
            SUM(tu.input_tokens) as input_tokens,
            SUM(tu.output_tokens) as output_tokens,
            SUM(tu.cache_creation_tokens) as cache_creation,
            SUM(tu.cache_read_tokens) as cache_read,
            ROUND(SUM(
                COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
            ), 4) as cost_usd
        FROM token_usage tu
        {join}
        {price_join}
        {where}
        GROUP BY DATE(tu.timestamp)
        ORDER BY date
    """, params if params else None)

    totals = run_query(conn, f"""
        SELECT
            COUNT(*) as total_calls,
            SUM(tu.input_tokens) as input_tokens,
            SUM(tu.output_tokens) as output_tokens,
            SUM(tu.cache_creation_tokens) as cache_creation,
            SUM(tu.cache_read_tokens) as cache_read,
            ROUND(SUM(COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0), 4) as cost_input,
            ROUND(SUM(COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0), 4) as cost_output,
            ROUND(SUM(COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0), 4) as cost_cache_write,
            ROUND(SUM(COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0), 4) as cost_cache_read,
            ROUND(SUM(
                COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
            ), 4) as cost_usd
        FROM token_usage tu
        {join}
        {price_join}
        {where}
    """, params if params else None)[0]

    # Q&A counts from messages
    msg_where, msg_params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='m'
    )
    msg_join = _join_session_sources('m')

    qa_counts = run_query(conn, f"""
        SELECT
            SUM(CASE WHEN type = 'user' THEN 1 ELSE 0 END) as questions,
            SUM(CASE WHEN type = 'assistant' THEN 1 ELSE 0 END) as answers
        FROM messages m
        {msg_join}
        {msg_where}
    """, msg_params if msg_params else None)[0]

    return {
        "by_model": by_model,
        "daily": daily,
        "totals": totals,
        "qa": qa_counts,
    }


# ===========================================================================
# FILE STATS
# ===========================================================================

def get_files_stats(conn, project_id=None, user_id=None, host_id=None,
                    start_date=None, end_date=None, client_source=None):
    """Get file operations statistics with 4D filters."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='fo'
    )
    join = _join_session_sources('fo')

    by_tool = run_query(conn, f"""
        SELECT tool, COUNT(*) as count
        FROM file_operations fo
        {join}
        {where}
        GROUP BY tool
        ORDER BY count DESC
    """, params if params else None)

    top_files = run_query(conn, f"""
        SELECT file_path, COUNT(*) as count
        FROM file_operations fo
        {join}
        {where}
        GROUP BY file_path
        ORDER BY count DESC
        LIMIT 20
    """, params if params else None)

    top_by_tool = run_query(conn, f"""
        SELECT file_path, tool, COUNT(*) as count
        FROM file_operations fo
        {join}
        {where}
        GROUP BY file_path, tool
        ORDER BY count DESC
        LIMIT 30
    """, params if params else None)

    daily = run_query(conn, f"""
        SELECT DATE(timestamp) as date, tool, COUNT(*) as count
        FROM file_operations fo
        {join}
        {where}
        GROUP BY DATE(timestamp), tool
        ORDER BY date
    """, params if params else None)

    hourly = run_query(conn, f"""
        SELECT
            CAST(strftime('%H', timestamp) AS INTEGER) as hour,
            COUNT(*) as count
        FROM file_operations fo
        {join}
        {where}
        GROUP BY hour
        ORDER BY hour
    """, params if params else None)

    totals = run_query(conn, f"""
        SELECT
            COUNT(*) as total_ops,
            COUNT(DISTINCT file_path) as unique_files,
            COUNT(DISTINCT fo.session_id) as sessions
        FROM file_operations fo
        {join}
        {where}
    """, params if params else None)[0]

    return {
        "by_tool": by_tool,
        "top_files": top_files,
        "top_by_tool": top_by_tool,
        "daily": daily,
        "hourly": hourly,
        "totals": totals,
    }


# ===========================================================================
# SKILLS / SUBAGENTS / MCP STATS
# ===========================================================================

def get_skills_stats(conn, project_id=None, user_id=None, host_id=None,
                     start_date=None, end_date=None, client_source=None):
    """Get skill invocation statistics with 4D filters."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='si'
    )
    join = _join_session_sources('si')

    top_skills = run_query(conn, f"""
        SELECT skill_name, COUNT(*) as count
        FROM skill_invocations si
        {join}
        {where}
        GROUP BY skill_name
        ORDER BY count DESC
        LIMIT 20
    """, params if params else None)

    daily = run_query(conn, f"""
        SELECT DATE(timestamp) as date, COUNT(*) as count
        FROM skill_invocations si
        {join}
        {where}
        GROUP BY DATE(timestamp)
        ORDER BY date
    """, params if params else None)

    return {"top": top_skills, "daily": daily}


def get_subagents_stats(conn, project_id=None, user_id=None, host_id=None,
                        start_date=None, end_date=None, client_source=None):
    """Get subagent invocation statistics with 4D filters."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='sa'
    )
    join = _join_session_sources('sa')

    top_subagents = run_query(conn, f"""
        SELECT subagent_type, COUNT(*) as count
        FROM subagent_invocations sa
        {join}
        {where}
        GROUP BY subagent_type
        ORDER BY count DESC
        LIMIT 20
    """, params if params else None)

    daily = run_query(conn, f"""
        SELECT DATE(timestamp) as date, COUNT(*) as count
        FROM subagent_invocations sa
        {join}
        {where}
        GROUP BY DATE(timestamp)
        ORDER BY date
    """, params if params else None)

    return {"top": top_subagents, "daily": daily}


def get_mcp_stats(conn, project_id=None, user_id=None, host_id=None,
                  start_date=None, end_date=None, client_source=None):
    """Get MCP tool call statistics with 4D filters."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='mt'
    )
    join = _join_session_sources('mt')

    by_server = run_query(conn, f"""
        SELECT server_name, COUNT(*) as count
        FROM mcp_tool_calls mt
        {join}
        {where}
        GROUP BY server_name
        ORDER BY count DESC
    """, params if params else None)

    top_tools = run_query(conn, f"""
        SELECT tool_name, server_name, COUNT(*) as count
        FROM mcp_tool_calls mt
        {join}
        {where}
        GROUP BY tool_name, server_name
        ORDER BY count DESC
        LIMIT 20
    """, params if params else None)

    daily = run_query(conn, f"""
        SELECT DATE(timestamp) as date, COUNT(*) as count
        FROM mcp_tool_calls mt
        {join}
        {where}
        GROUP BY DATE(timestamp)
        ORDER BY date
    """, params if params else None)

    return {"by_server": by_server, "top_tools": top_tools, "daily": daily}


# ===========================================================================
# BASH STATS
# ===========================================================================

def get_bash_stats(conn, project_id=None, user_id=None, host_id=None,
                   start_date=None, end_date=None, client_source=None):
    """Get bash command statistics with 4D filters."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='bc'
    )
    join = _join_session_sources('bc')

    by_type = run_query(conn, f"""
        SELECT
            CASE
                WHEN command LIKE 'cat %' OR command LIKE 'cat\\n%' THEN 'cat'
                WHEN command LIKE 'head %' THEN 'head'
                WHEN command LIKE 'tail %' THEN 'tail'
                WHEN command LIKE 'less %' THEN 'less'
                WHEN command LIKE 'sed %' THEN 'sed'
                WHEN command LIKE 'awk %' THEN 'awk'
                WHEN command LIKE 'cp %' THEN 'cp'
                WHEN command LIKE 'mv %' THEN 'mv'
                WHEN command LIKE 'rm %' THEN 'rm'
                WHEN command LIKE 'mkdir %' THEN 'mkdir'
                WHEN command LIKE 'touch %' THEN 'touch'
                WHEN command LIKE 'chmod %' THEN 'chmod'
                WHEN command LIKE 'ls %' OR command = 'ls' THEN 'ls'
                WHEN command LIKE 'find %' THEN 'find'
                WHEN command LIKE 'wc %' THEN 'wc'
                WHEN command LIKE 'sort %' THEN 'sort'
                WHEN command LIKE 'diff %' THEN 'diff'
                WHEN command LIKE 'tree %' OR command = 'tree' THEN 'tree'
                ELSE 'other'
            END as cmd_type,
            COUNT(*) as count
        FROM bash_commands bc
        {join}
        {where}
        GROUP BY cmd_type
        ORDER BY count DESC
    """, params if params else None)

    # Top target files
    file_where = where
    file_params = list(params)
    if file_where:
        file_where += " AND bc.target_file IS NOT NULL AND bc.target_file != ''"
    else:
        file_where = " WHERE bc.target_file IS NOT NULL AND bc.target_file != ''"

    top_files = run_query(conn, f"""
        SELECT target_file, COUNT(*) as count
        FROM bash_commands bc
        {join}
        {file_where}
        GROUP BY target_file
        ORDER BY count DESC
        LIMIT 20
    """, file_params if file_params else None)

    daily = run_query(conn, f"""
        SELECT DATE(timestamp) as date, COUNT(*) as count
        FROM bash_commands bc
        {join}
        {where}
        GROUP BY DATE(timestamp)
        ORDER BY date
    """, params if params else None)

    totals = run_query(conn, f"""
        SELECT
            COUNT(*) as total_commands,
            COUNT(DISTINCT target_file) as unique_files
        FROM bash_commands bc
        {join}
        {where}
    """, params if params else None)[0]

    return {
        "by_type": by_type,
        "top_files": top_files,
        "daily": daily,
        "totals": totals,
    }


# ===========================================================================
# SEARCH STATS
# ===========================================================================

def get_searches_stats(conn, project_id=None, user_id=None, host_id=None,
                       start_date=None, end_date=None, client_source=None):
    """Get search operations statistics with 4D filters."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='so'
    )
    join = _join_session_sources('so')

    by_tool = run_query(conn, f"""
        SELECT tool, COUNT(*) as count
        FROM search_operations so
        {join}
        {where}
        GROUP BY tool
        ORDER BY count DESC
    """, params if params else None)

    top_patterns = run_query(conn, f"""
        SELECT pattern, tool, COUNT(*) as count
        FROM search_operations so
        {join}
        {where}
        GROUP BY pattern, tool
        ORDER BY count DESC
        LIMIT 20
    """, params if params else None)

    path_where = where
    path_params = list(params)
    if path_where:
        path_where += " AND so.path IS NOT NULL AND so.path != ''"
    else:
        path_where = " WHERE so.path IS NOT NULL AND so.path != ''"

    top_paths = run_query(conn, f"""
        SELECT path, COUNT(*) as count
        FROM search_operations so
        {join}
        {path_where}
        GROUP BY path
        ORDER BY count DESC
        LIMIT 15
    """, path_params if path_params else None)

    daily = run_query(conn, f"""
        SELECT DATE(timestamp) as date, tool, COUNT(*) as count
        FROM search_operations so
        {join}
        {where}
        GROUP BY DATE(timestamp), tool
        ORDER BY date
    """, params if params else None)

    totals = run_query(conn, f"""
        SELECT
            COUNT(*) as total_searches,
            COUNT(DISTINCT pattern) as unique_patterns
        FROM search_operations so
        {join}
        {where}
    """, params if params else None)[0]

    return {
        "by_tool": by_tool,
        "top_patterns": top_patterns,
        "top_paths": top_paths,
        "daily": daily,
        "totals": totals,
    }


# ===========================================================================
# CLIENT / TIMELINE / DATE RANGE
# ===========================================================================

def get_client_stats(conn, project_id=None, user_id=None, host_id=None,
                     start_date=None, end_date=None, client_source=None):
    """Aggregate stats by client/source."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='tu'
    )
    join = _join_session_sources('tu')

    tokens_by_client = run_query(conn, f"""
        SELECT
            COALESCE(ss.source, 'unknown') as client_source,
            COUNT(*) as calls,
            SUM(tu.input_tokens) as input_tokens,
            SUM(tu.output_tokens) as output_tokens,
            SUM(tu.cache_creation_tokens) as cache_creation,
            SUM(tu.cache_read_tokens) as cache_read
        FROM token_usage tu
        {join}
        {where}
        GROUP BY client_source
        ORDER BY calls DESC
    """, params if params else None)

    msg_where, msg_params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='m'
    )
    msg_join = _join_session_sources('m')

    messages_by_client = run_query(conn, f"""
        SELECT
            COALESCE(ss.source, 'unknown') as client_source,
            SUM(CASE WHEN m.type = 'user' THEN 1 ELSE 0 END) as user_messages,
            SUM(CASE WHEN m.type = 'assistant' THEN 1 ELSE 0 END) as assistant_messages,
            COUNT(*) as total_messages
        FROM messages m
        {msg_join}
        {msg_where}
        GROUP BY client_source
        ORDER BY total_messages DESC
    """, msg_params if msg_params else None)

    fo_where, fo_params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='fo'
    )
    fo_join = _join_session_sources('fo')

    file_ops_by_client = run_query(conn, f"""
        SELECT
            COALESCE(ss.source, 'unknown') as client_source,
            COUNT(*) as file_ops
        FROM file_operations fo
        {fo_join}
        {fo_where}
        GROUP BY client_source
        ORDER BY file_ops DESC
    """, fo_params if fo_params else None)

    return {
        "tokens": tokens_by_client,
        "messages": messages_by_client,
        "file_ops": file_ops_by_client,
    }


def get_timeline_stats(conn, project_id=None, user_id=None, host_id=None,
                       start_date=None, end_date=None, client_source=None):
    """Get combined timeline data with 4D filters."""
    si_where, si_params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='si'
    )
    si_join = _join_session_sources('si')

    skills_daily = {r['date']: r['count'] for r in run_query(conn, f"""
        SELECT DATE(timestamp) as date, COUNT(*) as count
        FROM skill_invocations si
        {si_join}
        {si_where}
        GROUP BY DATE(timestamp)
    """, si_params if si_params else None)}

    sa_where, sa_params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='sa'
    )
    sa_join = _join_session_sources('sa')

    subagents_daily = {r['date']: r['count'] for r in run_query(conn, f"""
        SELECT DATE(timestamp) as date, COUNT(*) as count
        FROM subagent_invocations sa
        {sa_join}
        {sa_where}
        GROUP BY DATE(timestamp)
    """, sa_params if sa_params else None)}

    mt_where, mt_params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='mt'
    )
    mt_join = _join_session_sources('mt')

    mcp_daily = {r['date']: r['count'] for r in run_query(conn, f"""
        SELECT DATE(timestamp) as date, COUNT(*) as count
        FROM mcp_tool_calls mt
        {mt_join}
        {mt_where}
        GROUP BY DATE(timestamp)
    """, mt_params if mt_params else None)}

    all_dates = sorted(set(skills_daily.keys()) | set(subagents_daily.keys()) | set(mcp_daily.keys()))

    timeline = []
    for date in all_dates:
        timeline.append({
            "date": date,
            "skills": skills_daily.get(date, 0),
            "subagents": subagents_daily.get(date, 0),
            "mcp": mcp_daily.get(date, 0),
        })

    return timeline


def get_date_range(conn, project_id=None, user_id=None, host_id=None, client_source=None):
    """Get the date range of available data."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        client=client_source, table_alias='tu'
    )
    join = _join_session_sources('tu')

    result = run_query(conn, f"""
        SELECT
            MIN(tu.timestamp) as min_date,
            MAX(tu.timestamp) as max_date
        FROM token_usage tu
        {join}
        {where}
    """, params if params else None)[0]

    return {
        "min_date": result['min_date'][:10] if result['min_date'] else None,
        "max_date": result['max_date'][:10] if result['max_date'] else None,
    }


# ===========================================================================
# INTERACTIONS
# ===========================================================================

def get_interactions_list(conn, project_id=None, user_id=None, host_id=None,
                           search=None, start_date=None, end_date=None,
                           limit=50, offset=0, client_source=None,
                           classification=None, sensitivity=None):
    """Get list of interactions with 4D filters + optional metadata filters."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='c'
    )
    join = _join_session_sources('c')

    if search:
        # If search looks like a UUID/session_id, match directly and drop date
        # filters so the result is always found regardless of date range.
        import re
        if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-', search, re.IGNORECASE):
            extra = "c.session_id = ?"
            # Rebuild filters without date constraints
            where, params = build_filters(
                project_id=project_id, user_id=user_id, host_id=host_id,
                client=client_source, table_alias='c'
            )
        else:
            extra = """c.session_id IN (
                SELECT DISTINCT m.session_id FROM messages m
                JOIN messages_fts ON messages_fts.rowid = m.id
                WHERE messages_fts MATCH ?
            )"""
        if where:
            where += " AND " + extra
        else:
            where = " WHERE " + extra
        params.append(search)

    if classification:
        extra = "cm.classification = ?"
        if where:
            where += " AND " + extra
        else:
            where = " WHERE " + extra
        params.append(classification)

    if sensitivity:
        extra = "cm.data_sensitivity = ?"
        if where:
            where += " AND " + extra
        else:
            where = " WHERE " + extra
        params.append(sensitivity)

    interactions = run_query(conn, f"""
        SELECT
            c.session_id,
            c.project_id,
            c.user_id,
            c.host_id,
            c.timestamp,
            c.display,
            c.summary,
            c.project,
            c.model,
            c.total_tokens,
            c.message_count,
            c.tools_used,
            c.cwd,
            c.git_branch,
            c.parent_session_id,
            c.agent_id,
            COALESCE(ss.source, 'unknown') as client_source,
            p.name as project_name,
            u.username,
            h.hostname,
            cm.classification as meta_classification,
            cm.data_sensitivity as meta_sensitivity,
            cm.summary as meta_summary,
            COALESCE((
                SELECT ROUND(SUM(
                    COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
                ), 4)
                FROM token_usage tu
                LEFT JOIN model_pricing mp ON mp.model = tu.model
                    AND tu.timestamp >= mp.from_date
                    AND (mp.to_date IS NULL OR tu.timestamp < mp.to_date)
                WHERE tu.session_id = c.session_id AND tu.project_id = c.project_id
            ), 0) as cost_usd
        FROM interactions c
        {join}
        LEFT JOIN projects p ON p.id = c.project_id
        LEFT JOIN users u ON u.id = c.user_id
        LEFT JOIN hosts h ON h.id = c.host_id
        LEFT JOIN interaction_metadata cm ON cm.session_id = c.session_id AND cm.project_id = c.project_id
        {where}
        ORDER BY c.timestamp DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset])

    count_result = run_query(conn, f"""
        SELECT COUNT(*) as total
        FROM interactions c
        {join}
        LEFT JOIN interaction_metadata cm ON cm.session_id = c.session_id AND cm.project_id = c.project_id
        {where}
    """, params if params else None)
    total = count_result[0]['total'] if count_result else 0

    return {
        "interactions": interactions,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def search_messages(conn, query: str, limit: int = 20,
                    project_id=None, user_id=None, host_id=None, client_source=None,
                    classification=None, sensitivity=None, topic=None) -> list:
    """Search messages content using LIKE with wildcards.

    Supports filtering by classification metadata (classification, sensitivity, topic).
    """
    wildcard_query = f"%{query}%"

    where_clauses = ["m.content LIKE ?"]
    params = [wildcard_query]

    if project_id is not None:
        where_clauses.append("m.project_id = ?")
        params.append(project_id)
    if user_id is not None:
        where_clauses.append("m.user_id = ?")
        params.append(user_id)
    if host_id is not None:
        where_clauses.append("m.host_id = ?")
        params.append(host_id)
    if client_source:
        where_clauses.append("ss.source = ?")
        params.append(client_source)
    if classification:
        where_clauses.append("cm.classification = ?")
        params.append(classification)
    if sensitivity:
        where_clauses.append("cm.data_sensitivity = ?")
        params.append(sensitivity)
    if topic:
        where_clauses.append("cm.topics LIKE ?")
        params.append(f'%"{topic}"%')

    where = " WHERE " + " AND ".join(where_clauses)
    join = _join_session_sources('m')
    params.append(limit)

    results = run_query(conn, f"""
        SELECT
            m.session_id,
            m.project_id,
            c.timestamp,
            SUBSTR(m.content, 1, 200) as snippet,
            c.message_count,
            c.project,
            p.name as project_name,
            u.username,
            cm.classification as meta_classification,
            cm.data_sensitivity as meta_sensitivity,
            cm.summary as meta_summary,
            cm.process as meta_process
        FROM messages m
        JOIN interactions c ON m.session_id = c.session_id AND m.project_id = c.project_id
        LEFT JOIN projects p ON p.id = m.project_id
        LEFT JOIN users u ON u.id = m.user_id
        LEFT JOIN interaction_metadata cm
            ON cm.session_id = m.session_id AND cm.project_id = m.project_id
        {join}
        {where}
        GROUP BY m.session_id, m.project_id
        ORDER BY c.timestamp DESC
        LIMIT ?
    """, params)

    return results


def get_interaction_detail(conn, session_id, project_id=None):
    """Get full interaction transcript with parent info."""
    cost_subquery = """COALESCE((
                SELECT ROUND(SUM(
                    COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
                ), 4)
                FROM token_usage tu
                LEFT JOIN model_pricing mp ON mp.model = tu.model
                    AND tu.timestamp >= mp.from_date
                    AND (mp.to_date IS NULL OR tu.timestamp < mp.to_date)
                WHERE tu.session_id = c.session_id AND tu.project_id = c.project_id
            ), 0) as cost_usd"""
    if project_id is not None:
        conv = run_query(conn, f"""
            SELECT c.*, COALESCE(ss.source, 'unknown') as client_source,
                   p.name as project_name, u.username, h.hostname,
                   {cost_subquery}
            FROM interactions c
            LEFT JOIN session_sources ss ON ss.session_id = c.session_id AND ss.project_id = c.project_id
            LEFT JOIN projects p ON p.id = c.project_id
            LEFT JOIN users u ON u.id = c.user_id
            LEFT JOIN hosts h ON h.id = c.host_id
            WHERE c.session_id = ? AND c.project_id = ?
        """, [session_id, project_id])
    else:
        conv = run_query(conn, f"""
            SELECT c.*, COALESCE(ss.source, 'unknown') as client_source,
                   p.name as project_name, u.username, h.hostname,
                   {cost_subquery}
            FROM interactions c
            LEFT JOIN session_sources ss ON ss.session_id = c.session_id AND ss.project_id = c.project_id
            LEFT JOIN projects p ON p.id = c.project_id
            LEFT JOIN users u ON u.id = c.user_id
            LEFT JOIN hosts h ON h.id = c.host_id
            WHERE c.session_id = ?
        """, [session_id])

    if not conv:
        return None

    interaction = conv[0]
    pid = interaction.get('project_id')

    if interaction.get('parent_session_id'):
        parent = run_query(conn, """
            SELECT session_id, summary, display, timestamp
            FROM interactions WHERE session_id = ? AND project_id = ?
        """, [interaction['parent_session_id'], pid])
        interaction['parent_interaction'] = parent[0] if parent else None

    messages = run_query(conn, """
        SELECT uuid, type, content, timestamp, tokens_in, tokens_out, model
        FROM messages
        WHERE session_id = ? AND project_id = ?
        ORDER BY timestamp ASC
    """, [session_id, pid])

    return {
        "interaction": interaction,
        "messages": messages,
        "message_count": len(messages),
    }


# ===========================================================================
# USER / HOST / PROJECT LISTING
# ===========================================================================

def get_users_list(conn):
    """Get all users with stats, hosts, and top model."""
    users = run_query(conn, """
        SELECT
            u.id,
            u.username,
            u.display_name,
            u.first_seen,
            u.last_seen,
            COUNT(DISTINCT c.session_id) as total_sessions,
            COUNT(DISTINCT c.project_id) as total_projects,
            SUM(c.total_tokens) as total_tokens
        FROM users u
        LEFT JOIN interactions c ON c.user_id = u.id
        WHERE u.username != 'unknown'
        GROUP BY u.id
        ORDER BY total_sessions DESC
    """)

    # Enrich with hosts and top model per user
    for user in users:
        uid = user['id']
        hosts = run_query(conn, """
            SELECT DISTINCT h.hostname
            FROM interactions c
            JOIN hosts h ON h.id = c.host_id
            WHERE c.user_id = ?
        """, [uid])
        user['hosts'] = [h['hostname'] for h in hosts]

        top_model = run_query(conn, """
            SELECT model, SUM(input_tokens + output_tokens) as tokens
            FROM token_usage
            WHERE user_id = ?
            GROUP BY model
            ORDER BY tokens DESC
            LIMIT 1
        """, [uid])
        user['top_model'] = top_model[0]['model'] if top_model else None

    return users


def get_hosts_list(conn):
    """Get all hosts with stats."""
    return run_query(conn, """
        SELECT
            h.id,
            h.hostname,
            h.os_type,
            h.home_dir,
            h.first_seen,
            h.last_seen,
            COUNT(DISTINCT c.session_id) as total_sessions,
            COUNT(DISTINCT c.user_id) as total_users,
            SUM(c.total_tokens) as total_tokens
        FROM hosts h
        LEFT JOIN interactions c ON c.host_id = h.id
        WHERE h.hostname != 'unknown'
        GROUP BY h.id
        ORDER BY total_sessions DESC
    """)


def get_projects_list(conn, user_id=None, host_id=None):
    """Get all projects with stats, optionally filtered by user/host."""
    where_clauses = []
    params = []
    if user_id is not None:
        where_clauses.append("c.user_id = ?")
        params.append(user_id)
    if host_id is not None:
        where_clauses.append("c.host_id = ?")
        params.append(host_id)

    having = " HAVING " + " AND ".join(where_clauses) if where_clauses else ""
    # Actually we need WHERE not HAVING for this
    where = ""
    if where_clauses:
        where = " WHERE " + " AND ".join(where_clauses)

    rows = run_query(conn, f"""
        SELECT
            p.id,
            p.name,
            p.source_path,
            p.first_seen,
            p.last_seen,
            COUNT(DISTINCT c.session_id) as total_sessions,
            COUNT(DISTINCT c.user_id) as total_users,
            SUM(c.total_tokens) as total_tokens,
            GROUP_CONCAT(DISTINCT ss.source) as sources_csv
        FROM projects p
        LEFT JOIN interactions c ON c.project_id = p.id
        LEFT JOIN session_sources ss ON ss.session_id = c.session_id AND ss.project_id = c.project_id
        {where}
        GROUP BY p.id
        ORDER BY total_sessions DESC
    """, params if params else None)
    for row in rows:
        csv = row.pop('sources_csv', '') or ''
        row['sources'] = [s for s in csv.split(',') if s]
    return rows


def get_user_detail(conn, username):
    """Get detailed user stats."""
    user = run_query(conn, "SELECT * FROM users WHERE username = ?", [username])
    if not user:
        return None

    user_data = user[0]
    user_id = user_data['id']

    # Projects breakdown
    projects = run_query(conn, """
        SELECT
            p.name,
            COUNT(DISTINCT c.session_id) as sessions,
            SUM(c.total_tokens) as tokens
        FROM interactions c
        JOIN projects p ON p.id = c.project_id
        WHERE c.user_id = ?
        GROUP BY p.name
        ORDER BY sessions DESC
    """, [user_id])

    # Model breakdown
    models = run_query(conn, """
        SELECT model, COUNT(*) as calls,
               SUM(input_tokens + output_tokens) as tokens
        FROM token_usage
        WHERE user_id = ?
        GROUP BY model
        ORDER BY calls DESC
    """, [user_id])

    # Activity heatmap data (day_of_week x hour)
    heatmap = run_query(conn, """
        SELECT
            CAST(strftime('%w', timestamp) AS INTEGER) as day_of_week,
            CAST(strftime('%H', timestamp) AS INTEGER) as hour,
            COUNT(*) as count
        FROM token_usage
        WHERE user_id = ?
        GROUP BY day_of_week, hour
    """, [user_id])

    # Hosts used
    hosts = run_query(conn, """
        SELECT h.hostname, h.os_type, COUNT(DISTINCT c.session_id) as sessions
        FROM interactions c
        JOIN hosts h ON h.id = c.host_id
        WHERE c.user_id = ?
        GROUP BY h.hostname
    """, [user_id])

    return {
        "user": user_data,
        "projects": projects,
        "models": models,
        "heatmap": heatmap,
        "hosts": hosts,
    }


# ===========================================================================
# EXPORT (for agent/collector pull)
# ===========================================================================

# ===========================================================================
# INTERACTION METADATA (SQL classification)
# ===========================================================================

def get_interaction_metadata(conn, session_id, project_id=None):
    """Get SQL-based metadata for a single interaction."""
    if project_id is not None:
        rows = run_query(conn, """
            SELECT * FROM interaction_metadata
            WHERE session_id = ? AND project_id = ?
        """, [session_id, project_id])
    else:
        rows = run_query(conn, """
            SELECT * FROM interaction_metadata
            WHERE session_id = ?
        """, [session_id])

    if not rows:
        return None

    row = rows[0]
    # Parse JSON array fields
    import json
    for field in ('sensitive_data_types', 'topics', 'people', 'clients', 'tags'):
        val = row.get(field)
        if val and isinstance(val, str):
            try:
                row[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                row[field] = []
    return row


def get_classification_stats(conn, project_id=None, user_id=None, host_id=None,
                              start_date=None, end_date=None, client_source=None):
    """Get aggregated classification statistics."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='c'
    )
    join = _join_session_sources('c')

    by_classification = run_query(conn, f"""
        SELECT cm.classification, COUNT(*) as count
        FROM interaction_metadata cm
        JOIN interactions c ON c.session_id = cm.session_id AND c.project_id = cm.project_id
        {join}
        {where}
        GROUP BY cm.classification
        ORDER BY count DESC
    """, params if params else None)

    by_sensitivity = run_query(conn, f"""
        SELECT cm.data_sensitivity, COUNT(*) as count
        FROM interaction_metadata cm
        JOIN interactions c ON c.session_id = cm.session_id AND c.project_id = cm.project_id
        {join}
        {where}
        GROUP BY cm.data_sensitivity
        ORDER BY count DESC
    """, params if params else None)

    total_classified = run_query(conn, f"""
        SELECT COUNT(*) as total
        FROM interaction_metadata cm
        JOIN interactions c ON c.session_id = cm.session_id AND c.project_id = cm.project_id
        {join}
        {where}
    """, params if params else None)

    total_interactions = run_query(conn, f"""
        SELECT COUNT(*) as total
        FROM interactions c
        {join}
        {where}
    """, params if params else None)

    return {
        "by_classification": by_classification,
        "by_sensitivity": by_sensitivity,
        "total_classified": total_classified[0]["total"] if total_classified else 0,
        "total_interactions": total_interactions[0]["total"] if total_interactions else 0,
    }


def get_tagcloud_data(conn, project_id=None, user_id=None, host_id=None,
                       start_date=None, end_date=None, client_source=None):
    """Get frequency counts for topics, people, clients, processes.

    Returns dict with top items per category for tag cloud visualization.
    """
    import json as _json

    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='c'
    )
    join = _join_session_sources('c')

    rows = run_query(conn, f"""
        SELECT cm.topics, cm.people, cm.clients, cm.process
        FROM interaction_metadata cm
        JOIN interactions c ON c.session_id = cm.session_id AND c.project_id = cm.project_id
        {join}
        {where}
    """, params if params else None)

    counters = {"topics": {}, "people": {}, "clients": {}, "processes": {}}

    for row in rows:
        for field in ("topics", "people", "clients"):
            val = row.get(field, "")
            if val and isinstance(val, str):
                try:
                    items = _json.loads(val)
                except (ValueError, TypeError):
                    items = []
            elif isinstance(val, list):
                items = val
            else:
                items = []
            for item in items:
                if not isinstance(item, str):
                    continue
                item = item.strip()
                if item:
                    counters[field][item] = counters[field].get(item, 0) + 1

        process = (row.get("process") or "").strip()
        if process:
            counters["processes"][process] = counters["processes"].get(process, 0) + 1

    # Sort by frequency, top 100
    result = {}
    for key, counter in counters.items():
        sorted_items = sorted(counter.items(), key=lambda x: x[1], reverse=True)[:100]
        result[key] = [{"name": name, "count": count} for name, count in sorted_items]

    return result


# ===========================================================================
# COST INTELLIGENCE
# ===========================================================================

_COST_EXPR = """ROUND(
    COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
    + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
    + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
    + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
, 6)"""

_PRICE_JOIN = """LEFT JOIN model_pricing mp
    ON mp.model = tu.model
    AND tu.timestamp >= mp.from_date
    AND (mp.to_date IS NULL OR tu.timestamp < mp.to_date)"""


def get_session_cost_profile(conn, session_id, project_id=None):
    """Per-session cost analysis: summary, timeline, and phases."""
    pid_clause = "AND tu.project_id = ?" if project_id is not None else ""
    pid_params = [session_id, project_id] if project_id is not None else [session_id]

    rows = run_query(conn, f"""
        SELECT
            tu.timestamp,
            tu.model,
            COALESCE(tu.input_tokens, 0) as input_tokens,
            COALESCE(tu.output_tokens, 0) as output_tokens,
            COALESCE(tu.cache_creation_tokens, 0) as cache_creation_tokens,
            COALESCE(tu.cache_read_tokens, 0) as cache_read_tokens,
            COALESCE(tu.api_message_id, '') as api_message_id,
            {_COST_EXPR} as cost_usd
        FROM token_usage tu
        {_PRICE_JOIN}
        WHERE tu.session_id = ? {pid_clause}
        ORDER BY tu.timestamp ASC
    """, pid_params)

    if not rows:
        return None

    # Build timeline with cumulative cost
    cumulative = 0.0
    timeline = []
    total_input = total_output = total_cache_write = total_cache_read = 0
    total_cost = 0.0
    models = set()

    for r in rows:
        cost = r["cost_usd"] or 0.0
        cumulative += cost
        total_cost += cost
        total_input += r["input_tokens"]
        total_output += r["output_tokens"]
        total_cache_write += r["cache_creation_tokens"]
        total_cache_read += r["cache_read_tokens"]
        models.add(r["model"])
        timeline.append({
            "timestamp": r["timestamp"],
            "model": r["model"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cache_creation_tokens": r["cache_creation_tokens"],
            "cache_read_tokens": r["cache_read_tokens"],
            "api_message_id": r["api_message_id"],
            "cost_usd": round(cost, 6),
            "cumulative_cost": round(cumulative, 4),
        })

    # Duration
    first_ts = rows[0]["timestamp"] or ""
    last_ts = rows[-1]["timestamp"] or ""
    duration_minutes = 0
    if first_ts and last_ts and len(rows) > 1:
        from datetime import datetime
        try:
            fmt = "%Y-%m-%dT%H:%M:%S" if "T" in first_ts else "%Y-%m-%d %H:%M:%S"
            t0 = datetime.strptime(first_ts[:19], fmt)
            t1 = datetime.strptime(last_ts[:19], fmt)
            duration_minutes = round((t1 - t0).total_seconds() / 60, 1)
        except (ValueError, TypeError):
            pass

    # Phases: split into thirds
    n = len(timeline)
    third = max(n // 3, 1)
    slices = [timeline[:third], timeline[third:2*third], timeline[2*third:]]
    phase_names = ["early", "mid", "late"]
    phases = {}
    for name, sl in zip(phase_names, slices):
        phase_cost = sum(t["cost_usd"] for t in sl)
        phases[name] = {
            "cost": round(phase_cost, 4),
            "pct": round(phase_cost / total_cost * 100, 1) if total_cost > 0 else 0,
        }

    return {
        "summary": {
            "cost_usd": round(total_cost, 4),
            "duration_minutes": duration_minutes,
            "api_calls": len(rows),
            "models": sorted(models),
            "tokens": {
                "input": total_input,
                "output": total_output,
                "cache_creation": total_cache_write,
                "cache_read": total_cache_read,
            },
        },
        "timeline": timeline,
        "phases": phases,
    }


def get_work_pattern_stats(conn, project_id=None, user_id=None, host_id=None,
                           start_date=None, end_date=None, client_source=None):
    """Behavioral analysis: hourly, daily, complexity, session-length patterns."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='tu'
    )
    join = _join_session_sources('tu')

    # By hour of day
    by_hour = run_query(conn, f"""
        SELECT
            CAST(strftime('%H', tu.timestamp) AS INTEGER) as hour,
            COUNT(DISTINCT tu.session_id || '-' || tu.project_id) as session_count,
            ROUND(SUM({_COST_EXPR}), 4) as total_cost,
            ROUND(AVG({_COST_EXPR}), 4) as avg_cost,
            SUM(COALESCE(tu.input_tokens, 0) + COALESCE(tu.output_tokens, 0)
                + COALESCE(tu.cache_creation_tokens, 0) + COALESCE(tu.cache_read_tokens, 0)) as total_tokens
        FROM token_usage tu
        {join}
        {_PRICE_JOIN}
        {where}
        GROUP BY strftime('%H', tu.timestamp)
        ORDER BY hour
    """, params if params else None)

    # By day of week
    day_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    by_day_raw = run_query(conn, f"""
        SELECT
            CAST(strftime('%w', tu.timestamp) AS INTEGER) as day,
            COUNT(DISTINCT tu.session_id || '-' || tu.project_id) as session_count,
            ROUND(SUM({_COST_EXPR}), 4) as total_cost,
            ROUND(AVG({_COST_EXPR}), 4) as avg_cost
        FROM token_usage tu
        {join}
        {_PRICE_JOIN}
        {where}
        GROUP BY strftime('%w', tu.timestamp)
        ORDER BY day
    """, params if params else None)

    by_day_of_week = []
    for r in by_day_raw:
        d = r["day"]
        by_day_of_week.append({
            "day": d,
            "day_name": day_names[d] if 0 <= d <= 6 else str(d),
            "session_count": r["session_count"],
            "total_cost": r["total_cost"],
            "avg_cost": r["avg_cost"],
        })

    # By complexity bucket (API calls per session)
    # Need filter on interactions table for project/user/host, token_usage for calls
    i_where, i_params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='c'
    )
    i_join = _join_session_sources('c')

    by_complexity = run_query(conn, f"""
        SELECT
            bucket,
            COUNT(*) as count,
            ROUND(AVG(session_cost), 4) as avg_cost,
            ROUND(AVG(cache_hit_rate), 1) as avg_cache_hit_rate,
            ROUND(SUM(session_cost), 4) as total_cost
        FROM (
            SELECT
                c.session_id,
                c.project_id,
                COUNT(tu.rowid) as api_calls,
                CASE
                    WHEN COUNT(tu.rowid) < 10 THEN 'quick'
                    WHEN COUNT(tu.rowid) < 50 THEN 'medium'
                    ELSE 'deep'
                END as bucket,
                ROUND(SUM(
                    COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
                ), 4) as session_cost,
                CASE WHEN SUM(COALESCE(tu.input_tokens, 0) + COALESCE(tu.cache_read_tokens, 0) + COALESCE(tu.cache_creation_tokens, 0)) > 0
                    THEN ROUND(SUM(COALESCE(tu.cache_read_tokens, 0)) * 100.0
                        / SUM(COALESCE(tu.input_tokens, 0) + COALESCE(tu.cache_read_tokens, 0) + COALESCE(tu.cache_creation_tokens, 0)), 1)
                    ELSE 0
                END as cache_hit_rate
            FROM interactions c
            {i_join}
            JOIN token_usage tu ON tu.session_id = c.session_id AND tu.project_id = c.project_id
            {_PRICE_JOIN}
            {i_where}
            GROUP BY c.session_id, c.project_id
        )
        GROUP BY bucket
        ORDER BY CASE bucket WHEN 'quick' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
    """, i_params if i_params else None)

    # By session length (message count bins)
    by_session_length = run_query(conn, f"""
        SELECT
            bin_label as range,
            COUNT(*) as count,
            ROUND(AVG(session_cost), 4) as avg_cost
        FROM (
            SELECT
                session_id,
                project_id,
                msg_count,
                CASE
                    WHEN msg_count BETWEEN 1 AND 5 THEN '1-5'
                    WHEN msg_count BETWEEN 6 AND 15 THEN '6-15'
                    WHEN msg_count BETWEEN 16 AND 30 THEN '16-30'
                    WHEN msg_count BETWEEN 31 AND 60 THEN '31-60'
                    ELSE '60+'
                END as bin_label,
                CASE
                    WHEN msg_count BETWEEN 1 AND 5 THEN 1
                    WHEN msg_count BETWEEN 6 AND 15 THEN 2
                    WHEN msg_count BETWEEN 16 AND 30 THEN 3
                    WHEN msg_count BETWEEN 31 AND 60 THEN 4
                    ELSE 5
                END as bin_order,
                session_cost
            FROM (
                SELECT
                    c.session_id,
                    c.project_id,
                    (SELECT COUNT(*) FROM messages m WHERE m.session_id = c.session_id AND m.project_id = c.project_id) as msg_count,
                    COALESCE((
                        SELECT ROUND(SUM(
                            COALESCE(tu2.input_tokens, 0) * COALESCE(mp2.input_price_per_mtok, 0) / 1000000.0
                            + COALESCE(tu2.output_tokens, 0) * COALESCE(mp2.output_price_per_mtok, 0) / 1000000.0
                            + COALESCE(tu2.cache_creation_tokens, 0) * COALESCE(mp2.cache_write_price_per_mtok, 0) / 1000000.0
                            + COALESCE(tu2.cache_read_tokens, 0) * COALESCE(mp2.cache_read_price_per_mtok, 0) / 1000000.0
                        ), 4)
                        FROM token_usage tu2
                        LEFT JOIN model_pricing mp2 ON mp2.model = tu2.model
                            AND tu2.timestamp >= mp2.from_date
                            AND (mp2.to_date IS NULL OR tu2.timestamp < mp2.to_date)
                        WHERE tu2.session_id = c.session_id AND tu2.project_id = c.project_id
                    ), 0) as session_cost
                FROM interactions c
                {i_join}
                {i_where}
            ) sub
            WHERE msg_count > 0
        )
        GROUP BY bin_label
        ORDER BY bin_order
    """, i_params if i_params else None)

    return {
        "by_hour": by_hour,
        "by_day_of_week": by_day_of_week,
        "by_complexity_bucket": by_complexity,
        "by_session_length": by_session_length,
    }


def get_task_type_costs(conn, project_id=None, user_id=None, host_id=None,
                        start_date=None, end_date=None, client_source=None):
    """Classification-enriched cost analysis by task type."""
    i_where, i_params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='c'
    )
    i_join = _join_session_sources('c')

    # Count total and classified sessions
    counts = run_query(conn, f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN cm.session_id IS NOT NULL THEN 1 ELSE 0 END) as classified
        FROM interactions c
        {i_join}
        LEFT JOIN interaction_metadata cm ON cm.session_id = c.session_id AND cm.project_id = c.project_id
        {i_where}
    """, i_params if i_params else None)[0]

    total = counts["total"] or 0
    classified = counts["classified"] or 0
    has_classification = classified > 0

    data = []
    if has_classification:
        data = run_query(conn, f"""
            SELECT
                COALESCE(cm.classification, 'unclassified') as task_type,
                COUNT(DISTINCT c.session_id || '-' || c.project_id) as session_count,
                ROUND(AVG(session_cost), 4) as avg_cost,
                ROUND(SUM(session_cost), 4) as total_cost,
                ROUND(AVG(session_tokens), 0) as avg_tokens
            FROM (
                SELECT
                    c.session_id,
                    c.project_id,
                    COALESCE((
                        SELECT ROUND(SUM(
                            COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                            + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                            + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                            + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
                        ), 4)
                        FROM token_usage tu
                        LEFT JOIN model_pricing mp ON mp.model = tu.model
                            AND tu.timestamp >= mp.from_date
                            AND (mp.to_date IS NULL OR tu.timestamp < mp.to_date)
                        WHERE tu.session_id = c.session_id AND tu.project_id = c.project_id
                    ), 0) as session_cost,
                    COALESCE((
                        SELECT SUM(COALESCE(tu.input_tokens, 0) + COALESCE(tu.output_tokens, 0))
                        FROM token_usage tu
                        WHERE tu.session_id = c.session_id AND tu.project_id = c.project_id
                    ), 0) as session_tokens
                FROM interactions c
                {i_join}
                {i_where}
            ) c
            LEFT JOIN interaction_metadata cm ON cm.session_id = c.session_id AND cm.project_id = c.project_id
            GROUP BY COALESCE(cm.classification, 'unclassified')
            ORDER BY total_cost DESC
        """, i_params if i_params else None)

    return {
        "data": data,
        "classified_ratio": f"{classified}/{total}",
        "has_classification": has_classification,
    }


def get_efficiency_metrics(conn, project_id=None, user_id=None, host_id=None,
                           start_date=None, end_date=None, client_source=None):
    """Cost efficiency trends: cache, cost-per-call, model, source comparison."""
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='tu'
    )
    join = _join_session_sources('tu')

    # Daily cache trend
    cache_trend = run_query(conn, f"""
        SELECT
            DATE(tu.timestamp) as date,
            CASE WHEN SUM(COALESCE(tu.input_tokens, 0) + COALESCE(tu.cache_read_tokens, 0) + COALESCE(tu.cache_creation_tokens, 0)) > 0
                THEN ROUND(SUM(COALESCE(tu.cache_read_tokens, 0)) * 100.0
                    / SUM(COALESCE(tu.input_tokens, 0) + COALESCE(tu.cache_read_tokens, 0) + COALESCE(tu.cache_creation_tokens, 0)), 1)
                ELSE 0
            END as cache_hit_rate
        FROM token_usage tu
        {join}
        {where}
        GROUP BY DATE(tu.timestamp)
        ORDER BY date
    """, params if params else None)

    # Daily cost per call trend
    cost_per_call_trend = run_query(conn, f"""
        SELECT
            DATE(tu.timestamp) as date,
            ROUND(SUM(
                COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
            ) / NULLIF(COUNT(*), 0), 4) as avg_cost_per_call
        FROM token_usage tu
        {join}
        {_PRICE_JOIN}
        {where}
        GROUP BY DATE(tu.timestamp)
        ORDER BY date
    """, params if params else None)

    # Model efficiency (per-session averages)
    i_where, i_params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='c'
    )
    i_join = _join_session_sources('c')

    model_efficiency = run_query(conn, f"""
        SELECT
            model,
            ROUND(AVG(session_cost), 4) as avg_cost_per_session,
            ROUND(AVG(session_tokens), 0) as avg_tokens_per_session,
            COUNT(*) as session_count
        FROM (
            SELECT
                tu.model,
                c.session_id,
                c.project_id,
                ROUND(SUM(
                    COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
                ), 4) as session_cost,
                SUM(COALESCE(tu.input_tokens, 0) + COALESCE(tu.output_tokens, 0)) as session_tokens
            FROM interactions c
            {i_join}
            JOIN token_usage tu ON tu.session_id = c.session_id AND tu.project_id = c.project_id
            {_PRICE_JOIN}
            {i_where}
            GROUP BY tu.model, c.session_id, c.project_id
        )
        GROUP BY model
        ORDER BY session_count DESC
    """, i_params if i_params else None)

    # Source comparison (only if >1 source)
    source_comparison = run_query(conn, f"""
        SELECT
            COALESCE(ss.source, 'unknown') as source,
            ROUND(AVG(session_cost), 4) as avg_cost_per_session,
            COUNT(*) as session_count,
            ROUND(AVG(cache_hit_rate), 1) as avg_cache_hit_rate
        FROM (
            SELECT
                c.session_id,
                c.project_id,
                ROUND(SUM(
                    COALESCE(tu.input_tokens, 0) * COALESCE(mp.input_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.output_tokens, 0) * COALESCE(mp.output_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_creation_tokens, 0) * COALESCE(mp.cache_write_price_per_mtok, 0) / 1000000.0
                    + COALESCE(tu.cache_read_tokens, 0) * COALESCE(mp.cache_read_price_per_mtok, 0) / 1000000.0
                ), 4) as session_cost,
                CASE WHEN SUM(COALESCE(tu.input_tokens, 0) + COALESCE(tu.cache_read_tokens, 0) + COALESCE(tu.cache_creation_tokens, 0)) > 0
                    THEN ROUND(SUM(COALESCE(tu.cache_read_tokens, 0)) * 100.0
                        / SUM(COALESCE(tu.input_tokens, 0) + COALESCE(tu.cache_read_tokens, 0) + COALESCE(tu.cache_creation_tokens, 0)), 1)
                    ELSE 0
                END as cache_hit_rate
            FROM interactions c
            LEFT JOIN session_sources ss ON ss.session_id = c.session_id AND ss.project_id = c.project_id
            JOIN token_usage tu ON tu.session_id = c.session_id AND tu.project_id = c.project_id
            {_PRICE_JOIN}
            {i_where.replace("ss.source", "ss2.source") if False else i_where}
            GROUP BY c.session_id, c.project_id
        ) sub
        LEFT JOIN session_sources ss ON ss.session_id = sub.session_id AND ss.project_id = sub.project_id
        GROUP BY COALESCE(ss.source, 'unknown')
        ORDER BY session_count DESC
    """, i_params if i_params else None)

    # Only return source_comparison if >1 source
    if len(source_comparison) <= 1:
        source_comparison = []

    return {
        "cache_trend": cache_trend,
        "cost_per_call_trend": cost_per_call_trend,
        "model_efficiency": model_efficiency,
        "source_comparison": source_comparison,
    }


def generate_insights(work_patterns, task_type_costs, efficiency):
    """Generate 6-8 human-readable insight strings from aggregate data."""
    insights = []

    # 1. Expensive hour
    by_hour = work_patterns.get("by_hour", [])
    if by_hour:
        avg_hourly_cost = sum(h["total_cost"] or 0 for h in by_hour) / max(len(by_hour), 1)
        most_expensive = max(by_hour, key=lambda h: h["total_cost"] or 0)
        if avg_hourly_cost > 0 and (most_expensive["total_cost"] or 0) > avg_hourly_cost * 1.5:
            ratio = round((most_expensive["total_cost"] or 0) / avg_hourly_cost, 1)
            insights.append(f"Your most expensive hour is {most_expensive['hour']}:00 — {ratio}x your hourly average")

    # 2. Deep session cost
    by_complexity = work_patterns.get("by_complexity_bucket", [])
    quick = next((b for b in by_complexity if b["bucket"] == "quick"), None)
    deep = next((b for b in by_complexity if b["bucket"] == "deep"), None)
    if quick and deep and (quick["avg_cost"] or 0) > 0 and (deep["count"] or 0) > 0:
        ratio = round((deep["avg_cost"] or 0) / (quick["avg_cost"] or 1), 1)
        if ratio > 2:
            insights.append(f"Deep sessions (50+ calls) cost {ratio}x more than quick tasks on average")

    # 3. Cache trend
    cache_trend = efficiency.get("cache_trend", [])
    if len(cache_trend) >= 7:
        recent = cache_trend[-7:]
        older = cache_trend[:7]
        recent_avg = sum(d["cache_hit_rate"] or 0 for d in recent) / len(recent)
        older_avg = sum(d["cache_hit_rate"] or 0 for d in older) / len(older)
        if older_avg > 0:
            delta = recent_avg - older_avg
            if abs(delta) > 5:
                direction = "improved" if delta > 0 else "declined"
                insights.append(f"Cache hit rate {direction} from {round(older_avg)}% to {round(recent_avg)}% over the period")

    # 4. Source comparison
    source_comp = efficiency.get("source_comparison", [])
    if len(source_comp) >= 2:
        sorted_sources = sorted(source_comp, key=lambda s: s["avg_cost_per_session"] or 0)
        cheapest = sorted_sources[0]
        most_exp = sorted_sources[-1]
        if (most_exp["avg_cost_per_session"] or 0) > 0:
            pct_diff = round(((most_exp["avg_cost_per_session"] or 0) - (cheapest["avg_cost_per_session"] or 0))
                             / (most_exp["avg_cost_per_session"] or 1) * 100)
            if pct_diff > 15:
                insights.append(f"{cheapest['source']} sessions cost {pct_diff}% less than {most_exp['source']} on average")

    # 5. Cost per call trend
    cpc_trend = efficiency.get("cost_per_call_trend", [])
    if len(cpc_trend) >= 14:
        recent_half = cpc_trend[len(cpc_trend)//2:]
        older_half = cpc_trend[:len(cpc_trend)//2]
        recent_avg = sum(d["avg_cost_per_call"] or 0 for d in recent_half) / len(recent_half)
        older_avg = sum(d["avg_cost_per_call"] or 0 for d in older_half) / len(older_half)
        if older_avg > 0:
            pct_change = round((recent_avg - older_avg) / older_avg * 100)
            if abs(pct_change) > 10:
                direction = "increased" if pct_change > 0 else "decreased"
                insights.append(f"Your cost per API call has {direction} {abs(pct_change)}% in the recent period")

    # 6. Token type dominance (from model efficiency data)
    model_eff = efficiency.get("model_efficiency", [])
    # We don't have per-type breakdown in model_efficiency, skip if no data

    # 7. Day-of-week outlier
    by_day = work_patterns.get("by_day_of_week", [])
    if by_day:
        avg_daily = sum(d["total_cost"] or 0 for d in by_day) / max(len(by_day), 1)
        most_exp_day = max(by_day, key=lambda d: d["total_cost"] or 0)
        if avg_daily > 0 and (most_exp_day["total_cost"] or 0) > avg_daily * 1.5:
            ratio = round((most_exp_day["total_cost"] or 0) / avg_daily, 1)
            insights.append(f"{most_exp_day['day_name']} is your most expensive day — {ratio}x your daily average")

    # 8. Complexity sweet spot (best cache hit rate bucket)
    if len(by_complexity) >= 2:
        best_cache = max(by_complexity, key=lambda b: b["avg_cache_hit_rate"] or 0)
        if (best_cache["avg_cache_hit_rate"] or 0) > 30:
            bucket_ranges = {"quick": "<10", "medium": "10-50", "deep": "50+"}
            insights.append(
                f"{best_cache['bucket'].title()} sessions ({bucket_ranges.get(best_cache['bucket'], '')} API calls) "
                f"have the best cache hit rate ({round(best_cache['avg_cache_hit_rate'])}%) — sweet spot for cost efficiency"
            )

    return insights[:8]


def export_sessions(conn, since_ts: str, limit: int = 1000) -> list:
    """Export sessions with all related data since a timestamp.

    Batch-loads all child tables to avoid N+1 queries.
    Returns list of session dicts with nested data.
    """
    # Get interactions since timestamp
    interactions = run_query(conn, """
        SELECT c.*, p.name as project_name, u.username, h.hostname, h.os_type,
               COALESCE(ss.source, 'unknown') as client_source
        FROM interactions c
        LEFT JOIN projects p ON p.id = c.project_id
        LEFT JOIN users u ON u.id = c.user_id
        LEFT JOIN hosts h ON h.id = c.host_id
        LEFT JOIN session_sources ss ON ss.session_id = c.session_id AND ss.project_id = c.project_id
        WHERE c.timestamp > ?
        ORDER BY c.timestamp DESC
        LIMIT ?
    """, [since_ts, limit])

    if not interactions:
        return []

    # Collect all (session_id, project_id) pairs for batch loading
    session_keys = [(c["session_id"], c["project_id"]) for c in interactions]

    # Build IN clause for batch queries
    placeholders = ",".join(["(?,?)" for _ in session_keys])
    flat_params = []
    for sid, pid in session_keys:
        flat_params.extend([sid, pid])

    # Batch load all child tables
    def batch_load(table, extra_cols=""):
        rows = run_query(conn, f"""
            SELECT * FROM {table}
            WHERE (session_id, project_id) IN ({placeholders})
        """, flat_params)
        grouped = {}
        for r in rows:
            key = (r["session_id"], r["project_id"])
            grouped.setdefault(key, []).append(r)
        return grouped

    messages_map = batch_load("messages")
    tokens_map = batch_load("token_usage")
    files_map = batch_load("file_operations")
    bash_map = batch_load("bash_commands")
    search_map = batch_load("search_operations")
    skills_map = batch_load("skill_invocations")
    subagents_map = batch_load("subagent_invocations")
    mcp_map = batch_load("mcp_tool_calls")

    # Assemble sessions
    sessions = []
    for conv in interactions:
        key = (conv["session_id"], conv["project_id"])
        sessions.append({
            "interaction": conv,
            "messages": messages_map.get(key, []),
            "token_usage": tokens_map.get(key, []),
            "file_operations": files_map.get(key, []),
            "bash_commands": bash_map.get(key, []),
            "search_operations": search_map.get(key, []),
            "skill_invocations": skills_map.get(key, []),
            "subagent_invocations": subagents_map.get(key, []),
            "mcp_tool_calls": mcp_map.get(key, []),
        })

    return sessions

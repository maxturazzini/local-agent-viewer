"""
Predefined SQL queries for LocalAgentViewer statistics and analysis.

All queries support 4-dimensional filtering: project_id, user_id, host_id, source.
"""

import sqlite3
from collections import namedtuple


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
        # LAV-84. The unary `+` is NOT a typo and must not be "cleaned up".
        #
        # `ss` comes from _join_session_sources() as a LEFT JOIN, but this WHERE
        # term rejects NULL, so since SQLite 3.39 the planner strength-reduces it
        # to an INNER JOIN and is then free to reorder the two tables. With a bare
        # `ss.source = ?` it takes idx_session_sources_source as the outer loop and
        # probes the tool table once per matching session — 14.890 session_sources
        # rows x a per-project range scan:
        #
        #   file_operations  SEARCH ss USING idx_session_sources_source
        #                    -> SEARCH fo USING idx_fileops_project_ts      92,4 s
        #   bash_commands    same shape, SCAN over the covering index        117 s
        #
        # The unary `+` is value- and type-preserving; its only effect is that the
        # term can no longer drive an index, so the planner keeps the tool table as
        # the outer loop and probes session_sources by its primary key:
        #
        #   SCAN fo USING COVERING INDEX sqlite_autoindex_file_operations_1
        #   -> SEARCH ss USING sqlite_autoindex_session_sources_1            0,047 s
        #
        # Same rows out (70.538 / 67.717 measured here), ~2000x faster. If this ever
        # needs to become readable instead of clever, the equivalent rewrite is an
        # EXISTS subquery — but that needs its own build_filters variant, because
        # every caller currently assumes a pre-joined `ss`.
        clauses.append("+ss.source = ?")
        params.append(client)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _join_session_sources(table_alias='t'):
    """Return LEFT JOIN clause for session_sources."""
    return f"LEFT JOIN session_sources ss ON ss.session_id = {table_alias}.session_id AND ss.project_id = {table_alias}.project_id"


def _and_clause(where, clause):
    """AND an extra (parameter-free) condition onto a build_filters() WHERE.

    build_filters() returns "" when no filter is active, so the extra condition
    has to open the WHERE itself in that case. The clause is always a literal
    written here in code — never a user value — so the param list is untouched.
    """
    return (where + " AND " + clause) if where else (" WHERE " + clause)


# ===========================================================================
# TOOL OUTCOMES (LAV-78)
# ===========================================================================
#
# The six tool tables carry, since LAV-78:
#   is_error     NULL = no tool_result was ever seen | 0 = ok | 1 = error
#   duration_ms  wall clock call->result, NULL when not derivable
#   error_text   bash_commands + mcp_tool_calls only
#   exit_code    bash_commands only
#
# NO AGGREGATE OVER duration_ms IS EXPOSED HERE, on purpose. SQLite has no
# percentile function, and the mean is actively misleading on the measured
# distribution (p50 1.0s, p90 5.8s, p99 157s, max 76min — the tail is
# permission-prompt waits and background tasks, i.e. real but not "tool speed").
# The column stays available for ad-hoc SQL; publishing an average would put a
# number on the dashboard that nobody could interpret.


def _table_columns(conn, table):
    """Column names of `table`, or an empty set if it cannot be inspected.

    Used only to degrade gracefully on a DB where the LAV-78 migration has not
    run (or half-failed: init_db wraps migrations in try/except that prints and
    continues). Without this guard a missing column would raise inside
    /api/stats and take the whole dashboard down, not just the outcome numbers.
    """
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _outcome_agg(alias):
    """SELECT-list fragment: errors / measured / error_rate for a tool table.

    THE CORRECTNESS RULE OF THE WHOLE TICKET — the denominator is `measured`,
    never COUNT(*):

        measured   = SUM(CASE WHEN is_error IS NOT NULL THEN 1 ELSE 0 END)
        errors     = SUM(CASE WHEN is_error = 1        THEN 1 ELSE 0 END)
        error_rate = errors / measured

    is_error IS NULL does NOT mean success. It means no tool_result was ever
    observed for that call: truncated transcript, row not backfilled yet, or a
    source that emits no results at all (Codex). Dividing by COUNT(*) would fold
    every one of those rows into the denominator as an implicit success and
    deflate the rate — and the wrong number would still look perfectly
    plausible, which is exactly why it has to be spelled out here.

    error_rate is a fraction in [0, 1] rounded to 4 decimals, and is NULL (never
    0.0) when measured = 0: "not measured" is not "no errors". The guard is the
    outer CASE, so the division never runs on a zero denominator.

    The COALESCE wrappers matter for the ungrouped `totals` query: over an empty
    result set SUM() returns NULL, which would make `measured` come back as null
    ("unknown") when the truthful answer is 0 rows measured — and would leave the
    = 0 guard silently un-fired.

    `alias` is a table alias chosen in this module ('mt', 'bc') — never a user
    value; all filter values stay bound as parameters.
    """
    errors_sum = f"COALESCE(SUM(CASE WHEN {alias}.is_error = 1 THEN 1 ELSE 0 END), 0)"
    measured_sum = f"COALESCE(SUM(CASE WHEN {alias}.is_error IS NOT NULL THEN 1 ELSE 0 END), 0)"
    return f"""
            {errors_sum} as errors,
            {measured_sum} as measured,
            CASE
                WHEN {measured_sum} = 0 THEN NULL
                ELSE ROUND(1.0 * {errors_sum} / {measured_sum}, 4)
            END as error_rate"""


# Same three keys, all NULL: emitted when the LAV-78 columns are not in the DB
# yet. NULL = "unknown" and keeps the response shape stable for the frontend;
# a 0 here would be indistinguishable from a genuine "no errors".
_OUTCOME_AGG_MISSING = """
            NULL as errors,
            NULL as measured,
            NULL as error_rate"""


# ===========================================================================
# ERROR TAXONOMY (LAV-85)
# ===========================================================================
#
# `is_error = 1` is not the same thing as "the tool ran and failed". Measured
# over the 7.245 bash errors on prod, 1.838 of them — 25,4% — are calls that
# NEVER EXECUTED: 1.080 permission refusals, 713 cancelled siblings (a parallel
# call in the same block failed, so this one was never dispatched), 45
# interruptions. Counting those as failures overstates the Bash error rate from
# ~5,2% to 6,9% and, worse, ranks tools by how often the user declines a prompt
# rather than by how often they break.
#
# So every error gets a CLASS, and the three "never executed" classes can be
# excluded from both numerator and denominator: a call that never ran can
# neither succeed nor fail, so it belongs in neither.
#
# The markers below are not guesses — they are the literal strings the harness
# writes, taken from the real corpus with their frequencies:
#   "The user doesn't want to proceed with this tool use"                273
#   "<tool_use_error>Sibling tool call errored</tool_use_error>"         145
#   "Permission to use Bash has been denied. IMPORTANT: ..."              80
#   "Tool permission request failed: Error: Stream closed"                33
#   "Permission to use Bash with command ..."                             45+
#   "Permission to use Bash has been auto-denied (prompts unavailable)"   29
#   "Permission for this action was denied by the ... auto mode classifier" 36
#   "<tool_use_error>Cancelled: parallel tool call Bash(...)"             12
#   "Permission for this tool use was denied"                              9

ERROR_CLASS_SIBLING = "sibling"          # a parallel call failed -> never dispatched
ERROR_CLASS_DENIED = "denied"            # permission refused / blocked -> never ran
ERROR_CLASS_INTERRUPTED = "interrupted"  # aborted / prompt stream broke -> never ran
ERROR_CLASS_REJECTED = "rejected"        # malformed or unavailable call -> never ran
ERROR_CLASS_FAILED = "failed"            # it RAN and failed. The only real failure.
ERROR_CLASS_UNKNOWN = "unknown"          # is_error = 1 but no text to classify by

# Classes whose calls never executed. Excluded from BOTH sides of the real error
# rate. Single source of truth for SQL, JSON and any frontend legend.
ERROR_CLASSES_NEVER_EXECUTED = (ERROR_CLASS_SIBLING, ERROR_CLASS_DENIED,
                                ERROR_CLASS_INTERRUPTED, ERROR_CLASS_REJECTED)

ERROR_CLASSES = (ERROR_CLASS_FAILED, ERROR_CLASS_DENIED, ERROR_CLASS_SIBLING,
                 ERROR_CLASS_INTERRUPTED, ERROR_CLASS_REJECTED, ERROR_CLASS_UNKNOWN)

# ORDER IS LOAD-BEARING, not alphabetical. A cancelled-sibling payload quotes the
# command that was cancelled and can therefore contain the word "Permission";
# sibling must win. Likewise a denial whose prompt stream then closed must read
# as denied, not interrupted.
#
# The marker set was TUNED AGAINST THE CORPUS, not guessed: a first pass left
# 2.756 rows in `failed`, and reading the ones with no exit_code surfaced four
# whole families that had been missed — "doesn't want to TAKE THIS ACTION" (the
# variant of the most common denial), "was blocked. For security ...",
# "<tool_use_error>Blocked: sleep ...", and "requires approval". Re-tune the same
# way: filter on class = 'failed' AND exit_code IS NULL and read what is left.
_ERROR_CLASS_MARKERS = (
    (ERROR_CLASS_SIBLING, (
        "Sibling tool call errored",
        "Cancelled: parallel tool call",
    )),
    (ERROR_CLASS_DENIED, (
        # "doesn't want to" covers both "...to proceed with this tool use" and
        # "...to take this action right now" — two spellings of one refusal.
        "doesn't want to",
        "Permission to use",
        "Permission for this",
        "auto-denied",
        "was denied by the",
        "was blocked",
        "Blocked:",
        "requires approval",
        "require approval",
        # "...operations require EXPLICIT approval..." — the adverb defeats the
        # marker above, which is how this family was missed on the first pass.
        "require explicit approval",
        # The harness's own preamble to a security refusal; catches the whole
        # family regardless of how the sentence before it is phrased.
        "For security, Claude Code",
    )),
    (ERROR_CLASS_INTERRUPTED, (
        "Tool permission request failed",
        "Request interrupted",
        "AbortError",
        # The safety classifier could not be reached, so the call was never let
        # through: not the tool's fault, and not the user's either.
        "auto mode cannot determine",
    )),
    (ERROR_CLASS_REJECTED, (
        "InputValidationError",
        "Failed to parse command",
        "No such tool available",
    )),
)


def _error_class_expr(alias, has_error_text=True):
    """SQL CASE mapping one row to an ERROR_CLASS_*, or NULL when not an error.

    NULL for is_error 0 or NULL is deliberate and mirrors _outcome_agg: a row
    that never failed has no error class, and a row that was never measured has
    no class either — neither is 'failed'.

    instr(), never LIKE: these are exact substrings and LIKE would give '_' and
    '%' inside a harness message a wildcard meaning.

    `has_error_text=False` collapses to 'unknown' for every error — the honest
    answer for a table (or a DB) without the column, and the reason
    `never_executed` must be reported as NULL rather than 0 for those.

    `alias` and every marker are literals from this module; no user value is
    interpolated.
    """
    if not has_error_text:
        return (f"CASE WHEN {alias}.is_error = 1 THEN '{ERROR_CLASS_UNKNOWN}' "
                f"ELSE NULL END")

    branches = [f"WHEN {alias}.is_error IS NULL OR {alias}.is_error = 0 THEN NULL"]
    for cls, markers in _ERROR_CLASS_MARKERS:
        # SQL-escape the apostrophe: the most frequent marker of all is
        # "doesn't want to proceed" (273 rows), and an unescaped ' terminates
        # the literal and produces a syntax error at query time, not import
        # time. The markers are module constants, never user input — this is
        # correctness, not injection defence.
        test = " OR ".join(
            "instr({a}.error_text, '{m}') > 0".format(a=alias, m=m.replace("'", "''"))
            for m in markers)
        branches.append(f"WHEN {test} THEN '{cls}'")
    branches.append(
        f"WHEN {alias}.error_text IS NULL OR {alias}.error_text = '' "
        f"THEN '{ERROR_CLASS_UNKNOWN}'")
    branches.append(f"ELSE '{ERROR_CLASS_FAILED}'")
    return "CASE " + " ".join(branches) + " END"


def _never_executed_agg(alias, has_error_text=True):
    """SELECT fragment: `never_executed` + `errors_unclassified` for a group.

    TWO numbers, and the second is not optional. `never_executed` alone is a
    trap in the window between the migration (which adds error_text) and the
    backfill (which fills it): the column exists, every historical row holds '',
    every error classifies as `unknown`, and never_executed comes back 0 —
    reading as "none of these were denied/cancelled" when the truth is "we have
    not looked yet". `errors_unclassified` is what lets a caller tell those two
    apart, and it is why the UI can say "N of M errors carry no text".

    NULL — not 0 — for a table with no error_text at all: "we cannot tell" must
    not render as "none of them", the same trap as counting is_error NULL as
    success.
    """
    if not has_error_text:
        return "NULL as never_executed, NULL as errors_unclassified"
    cls = _error_class_expr(alias)
    marks = ", ".join(f"'{c}'" for c in ERROR_CLASSES_NEVER_EXECUTED)
    return (f"COALESCE(SUM(CASE WHEN {cls} IN ({marks}) THEN 1 ELSE 0 END), 0) "
            f"as never_executed, "
            f"COALESCE(SUM(CASE WHEN {cls} = '{ERROR_CLASS_UNKNOWN}' THEN 1 ELSE 0 END), 0) "
            f"as errors_unclassified")


# ===========================================================================
# COST EXPRESSIONS
# ===========================================================================
#
# The only place that turns a token_usage row and its model_pricing row into USD.
# Costs are calculated at query time, never materialized, so a second copy of this
# formula is two pages showing two numbers for the same session.

_CostExprs = namedtuple("_CostExprs", "input output cache_write cache_read total")


def _cost_exprs(tu="tu", mp="mp"):
    """Unrounded SQL expressions pricing one token_usage row, in USD.

    `tu` and `mp` are the aliases of token_usage and of the model_pricing row joined
    to it (see _price_join). Returns the four components and `total`, their sum.

    LAV-96: cache writes are priced in two parts: the 5-minute ones at
    cache_write_price_per_mtok (1.25x input) and cache_creation_1h_tokens at 2x
    input, Anthropic's 1-hour rate.

    LAV-98: long-context tier, per single request. The prompt length is input + cache
    read + cache write; strictly above long_context_threshold the whole request pays
    the lc_* prices, cache hits included, and the 2x of the 1-hour write is on the
    input price of the tier applied. Codex rows store net input, so the sum is the
    prompt length for every provider. NULL threshold or no pricing row: base prices,
    0 for an unpriced model (<synthetic>, empty model). NULL lc_*: the base price of
    that component.
    """
    prompt_tokens = (f"COALESCE({tu}.input_tokens, 0) + COALESCE({tu}.cache_read_tokens, 0)"
                     f" + COALESCE({tu}.cache_creation_tokens, 0)")

    def price(base, long_context):
        # IS NOT NULL is redundant in logic (x > NULL is not true) but skips the sum
        # on every row without a tier: get_token_stats goes from +35% to +7% over
        # the formula without tiers.
        return (f"COALESCE(CASE WHEN {mp}.long_context_threshold IS NOT NULL"
                f" AND {prompt_tokens} > {mp}.long_context_threshold"
                f" THEN COALESCE({mp}.{long_context}, {mp}.{base})"
                f" ELSE {mp}.{base} END, 0)")

    input_price = price("input_price_per_mtok", "lc_input")
    output_price = price("output_price_per_mtok", "lc_output")
    cache_write_price = price("cache_write_price_per_mtok", "lc_cache_write")
    cache_read_price = price("cache_read_price_per_mtok", "lc_cache_read")

    cost_input = f"COALESCE({tu}.input_tokens, 0) * {input_price} / 1000000.0"
    cost_output = f"COALESCE({tu}.output_tokens, 0) * {output_price} / 1000000.0"
    cost_cache_write = (
        f"((COALESCE({tu}.cache_creation_tokens, 0) - COALESCE({tu}.cache_creation_1h_tokens, 0))"
        f" * {cache_write_price}"
        f" + COALESCE({tu}.cache_creation_1h_tokens, 0) * 2 * {input_price}) / 1000000.0")
    cost_cache_read = f"COALESCE({tu}.cache_read_tokens, 0) * {cache_read_price} / 1000000.0"
    return _CostExprs(
        input=f"({cost_input})",
        output=f"({cost_output})",
        cache_write=f"({cost_cache_write})",
        cache_read=f"({cost_cache_read})",
        total=f"({cost_input} + {cost_output} + {cost_cache_write} + {cost_cache_read})",
    )


def _price_join(tu="tu", mp="mp"):
    """LEFT JOIN of a token_usage row to the price valid at its timestamp."""
    return f"""LEFT JOIN model_pricing {mp}
    ON {mp}.model = {tu}.model
    AND {tu}.timestamp >= {mp}.from_date
    AND ({mp}.to_date IS NULL OR {tu}.timestamp < {mp}.to_date)"""


_COST = _cost_exprs()
_COST_EXPR = f"ROUND({_COST.total}, 6)"
_PRICE_JOIN = _price_join()


def _session_cost_sum(tu, mp):
    """Scalar subquery, correlated on `c`: NULL for a session with no token_usage row."""
    return f"""(
                SELECT SUM({_cost_exprs(tu, mp).total})
                FROM token_usage {tu}
                {_price_join(tu, mp)}
                WHERE {tu}.session_id = c.session_id AND {tu}.project_id = c.project_id
            )"""


def _per_session_cost_subquery(tu="tu", mp="mp"):
    """The per-session cost SQL shape reused across list/detail/children queries.

    Correlated on the outer interactions row `c`. Rounded to 4 decimals, 0 for a
    session with no token_usage row.
    """
    return f"COALESCE(ROUND({_session_cost_sum(tu, mp)}, 4), 0)"


def _round_cost(usd):
    """A sum of costs to 4 decimals, the same whatever order it was added up in.

    The one rounding of everything that adds sessions or rows together: summing
    rounded costs accumulates the rounding. Prices have few decimals, so exact ties
    are common (at Opus 5.5 prices one session in 500 costs exactly x.xxxx5): there
    float noise decides the last digit, and list and detail add the same rows in
    different orders. The first round takes the noise away: no price has more than 4
    decimals, so no cost has more than 10.
    """
    return round(round(usd, 10), 4)


# Sessions in one IN list: an old SQLite takes 999 variables, project ids included.
_MAX_IN_SESSIONS = 500


def _in_chunks(values):
    """A sorted list of values, in slices that fit an IN list."""
    values = sorted(values)
    return [values[i:i + _MAX_IN_SESSIONS] for i in range(0, len(values), _MAX_IN_SESSIONS)]


def _sessions_cost(conn, keys, rounded=False):
    """Cost of many sessions at once: {(session_id, project_id): USD}.

    Every per-session subquery scans the rows of a project, so one per session is what
    made the pages with many subagents slow: this is one scan for all of them. A session
    with no token_usage row is missing from the result. Unrounded for adding sessions
    together; rounded=True is the value of _per_session_cost_subquery().
    """
    keys = set(keys)
    project_ids = sorted({project_id for _, project_id in keys})
    cost_sql = f"ROUND(SUM({_COST.total}), 4)" if rounded else f"SUM({_COST.total})"
    costs = {}
    for session_ids in _in_chunks({session_id for session_id, _ in keys}):
        rows = run_query(conn, f"""
            SELECT tu.session_id, tu.project_id, {cost_sql} as cost_usd
            FROM token_usage tu
            {_PRICE_JOIN}
            WHERE tu.project_id IN ({",".join("?" for _ in project_ids)})
              AND tu.session_id IN ({",".join("?" for _ in session_ids)})
            GROUP BY tu.session_id, tu.project_id
        """, project_ids + session_ids)
        for row in rows:
            key = (row['session_id'], row['project_id'])
            # The two IN lists cross: a copy of a session nobody asked for comes back too.
            if key in keys:
                costs[key] = row['cost_usd']
    return costs


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

    by_model = run_query(conn, f"""
        SELECT
            tu.model as model,
            COUNT(*) as calls,
            SUM(tu.input_tokens) as input_tokens,
            SUM(tu.output_tokens) as output_tokens,
            SUM(tu.cache_creation_tokens) as cache_creation,
            SUM(tu.cache_read_tokens) as cache_read,
            ROUND(SUM({_COST.input}), 4) as cost_input,
            ROUND(SUM({_COST.output}), 4) as cost_output,
            ROUND(SUM({_COST.cache_write}), 4) as cost_cache_write,
            ROUND(SUM({_COST.cache_read}), 4) as cost_cache_read,
            ROUND(SUM({_COST.total}), 4) as cost_usd,
            MAX(mp.id IS NOT NULL) as has_price
        FROM token_usage tu
        {join}
        {_PRICE_JOIN}
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
            ROUND(SUM({_COST.total}), 4) as cost_usd
        FROM token_usage tu
        {join}
        {_PRICE_JOIN}
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
            ROUND(SUM({_COST.input}), 4) as cost_input,
            ROUND(SUM({_COST.output}), 4) as cost_output,
            ROUND(SUM({_COST.cache_write}), 4) as cost_cache_write,
            ROUND(SUM({_COST.cache_read}), 4) as cost_cache_read,
            ROUND(SUM({_COST.total}), 4) as cost_usd
        FROM token_usage tu
        {join}
        {_PRICE_JOIN}
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

    # LAV-85: same graceful-degradation guard as get_mcp_stats — on a DB where
    # the migration has not run (or half-failed) the outcome keys come back NULL
    # instead of taking the whole /api/data response down.
    fo_cols = _table_columns(conn, 'file_operations')
    has_outcome = 'is_error' in fo_cols
    has_error_text = 'error_text' in fo_cols
    outcome_agg = _outcome_agg('fo') if has_outcome else _OUTCOME_AGG_MISSING
    never_agg = (_never_executed_agg('fo', has_error_text) if has_outcome
                 else "NULL as never_executed, NULL as errors_unclassified")

    by_tool = run_query(conn, f"""
        SELECT tool, COUNT(*) as count,
            {outcome_agg},
            {never_agg}
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
            COUNT(DISTINCT fo.session_id) as sessions,
            {outcome_agg},
            {never_agg}
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

    si_cols = _table_columns(conn, 'skill_invocations')
    has_outcome = 'is_error' in si_cols
    outcome_agg = _outcome_agg('si') if has_outcome else _OUTCOME_AGG_MISSING
    never_agg = (_never_executed_agg('si', 'error_text' in si_cols) if has_outcome
                 else "NULL as never_executed, NULL as errors_unclassified")

    top_skills = run_query(conn, f"""
        SELECT skill_name, COUNT(*) as count,
            {outcome_agg},
            {never_agg}
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

    sa_cols = _table_columns(conn, 'subagent_invocations')
    has_outcome = 'is_error' in sa_cols
    has_spawn = 'spawn_tool' in sa_cols
    outcome_agg = _outcome_agg('sa') if has_outcome else _OUTCOME_AGG_MISSING
    never_agg = (_never_executed_agg('sa', 'error_text' in sa_cols) if has_outcome
                 else "NULL as never_executed, NULL as errors_unclassified")
    # LAV-82: '' is the honest value for the five rows written between the
    # Task->Agent rename and this ticket, so it is surfaced as its own bucket
    # rather than folded into 'Task'.
    spawn_expr = ("COALESCE(NULLIF(sa.spawn_tool, ''), '(unknown)')" if has_spawn
                  else "'(unknown)'")

    top_subagents = run_query(conn, f"""
        SELECT subagent_type, COUNT(*) as count,
            {outcome_agg},
            {never_agg}
        FROM subagent_invocations sa
        {join}
        {where}
        GROUP BY subagent_type
        ORDER BY count DESC
        LIMIT 20
    """, params if params else None)

    # LAV-82: which tool generation spawned these. The Task/Agent split is the
    # only way to see, from the dashboard, that the table was frozen for months.
    by_spawn_tool = run_query(conn, f"""
        SELECT {spawn_expr} as spawn_tool, COUNT(*) as count,
            {outcome_agg}
        FROM subagent_invocations sa
        {join}
        {where}
        GROUP BY spawn_tool
        ORDER BY count DESC
    """, params if params else None)

    daily = run_query(conn, f"""
        SELECT DATE(timestamp) as date, COUNT(*) as count
        FROM subagent_invocations sa
        {join}
        {where}
        GROUP BY DATE(timestamp)
        ORDER BY date
    """, params if params else None)

    return {"top": top_subagents, "daily": daily, "by_spawn_tool": by_spawn_tool}


def get_mcp_stats(conn, project_id=None, user_id=None, host_id=None,
                  start_date=None, end_date=None, client_source=None):
    """Get MCP tool call statistics with 4D filters.

    Outcome columns (LAV-78) add `errors`, `measured` and `error_rate` to
    by_server / top_tools, plus a `top_errors` list. See _outcome_agg() for why
    the denominator is `measured` and not COUNT(*).
    """
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='mt'
    )
    join = _join_session_sources('mt')

    # Degrade gracefully on a DB that has not been migrated yet.
    mcp_cols = _table_columns(conn, 'mcp_tool_calls')
    has_outcome = 'is_error' in mcp_cols
    outcome_agg = _outcome_agg('mt') if has_outcome else _OUTCOME_AGG_MISSING
    error_text_expr = "mt.error_text" if 'error_text' in mcp_cols else "''"
    # LAV-85: all six tool tables expose the same outcome shape, so the
    # cross-table leaderboard can UNION them without special-casing any of them.
    never_agg = (_never_executed_agg('mt', 'error_text' in mcp_cols) if has_outcome
                 else "NULL as never_executed, NULL as errors_unclassified")

    by_server = run_query(conn, f"""
        SELECT
            server_name,
            COUNT(*) as count,
            {outcome_agg},
            {never_agg}
        FROM mcp_tool_calls mt
        {join}
        {where}
        GROUP BY server_name
        ORDER BY count DESC
    """, params if params else None)

    top_tools = run_query(conn, f"""
        SELECT
            tool_name,
            server_name,
            COUNT(*) as count,
            {outcome_agg},
            {never_agg}
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

    # Which MCP tools actually fail, with the most recent error message.
    # `is_error = 1` already excludes both successes (0) and never-measured
    # rows (NULL), so no extra guard is needed on the denominator here.
    # last_error_text is a bare column paired with the query's single MAX()
    # aggregate: SQLite then takes it from the row that produced that MAX
    # (documented behaviour since 3.7.11), i.e. the most recent error.
    top_errors = []
    if has_outcome:
        err_where = _and_clause(where, "mt.is_error = 1")
        top_errors = run_query(conn, f"""
            SELECT
                tool_name,
                server_name,
                COUNT(*) as errors,
                MAX(mt.timestamp) as last_error_at,
                {error_text_expr} as last_error_text
            FROM mcp_tool_calls mt
            {join}
            {err_where}
            GROUP BY tool_name, server_name
            ORDER BY errors DESC, last_error_at DESC
            LIMIT 20
        """, params if params else None)

    return {
        "by_server": by_server,
        "top_tools": top_tools,
        "daily": daily,
        "top_errors": top_errors,
    }


# ===========================================================================
# BASH STATS
# ===========================================================================

# FALLBACK ONLY since LAV-79. Command-type bucketing by LIKE prefix, shared by
# by_type and top_errors so the two can never drift apart. Kept byte-identical to
# the expression that used to be inlined in get_bash_stats (including the
# 'cat\\n%' pattern, which SQLite reads literally — LIKE has no default escape
# character).
#
# Why it is no longer the primary path: it only ever knew 18 programs plus
# 'other', which was survivable while bash_commands held the file-related 24.8%
# of Bash and nothing else. LAV-79 records 100% of shell, and against the full
# population 69% of the rows land in 'other' — the largest bucket by 3x, i.e. one
# grey slice covering exactly the 47k commands the ticket just recovered. The
# real answer is bash_commands.cmd_name (derived by jsonl.bash_cmd_name at parse
# time, backfilled in place by _migrate_add_cmd_name); this CASE survives only so
# that a DB where that migration has not run — or half-failed, since init_db
# prints and continues — still renders a chart instead of raising inside
# /api/stats and taking the whole dashboard down.
_BASH_CMD_TYPE_CASE = """CASE
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
            END"""

# LAV-79. cmd_name has 303 distinct values over the full Bash population (63,978
# calls measured on this host), and a 303-slice chart is not a chart. by_type
# keeps the top N and folds the tail into a single explicit roll-up row, so
# SUM(by_type.count) still equals totals.total_commands. A bare LIMIT with no
# roll-up would silently drop the 288 remaining commands — 19% of the volume and
# 30% of the errors — off the bottom of the chart, with nothing on screen saying
# so.
_BASH_BY_TYPE_LIMIT = 15

# Display label for a row whose cmd_name is NULL ("not derived yet" — a row the
# in-place backfill has not reached, only visible on a half-migrated DB) or ''
# ("derived and undecidable" — 6 rows in 63,978 here). The distinction matters on
# disk and is meaningless to a reader, so the two are folded together in SQL
# rather than shipped as two indistinguishable buckets, one of them labelled
# `null` in the JSON.
_BASH_CMD_UNKNOWN = "(unknown)"

# Label of the tail roll-up row appended by _fold_bash_by_type().
_BASH_CMD_OTHER = "other"


def _bash_cmd_name_expr(alias='bc'):
    """GROUP BY / SELECT expression for the LAV-79 cmd_name column.

    Both `alias` and _BASH_CMD_UNKNOWN are literals written in this module — no
    user value reaches the f-string; every filter value stays bound as a
    parameter. (_BASH_CMD_UNKNOWN deliberately contains no quote character, so it
    is safe to inline as a SQL string literal.)
    """
    return f"COALESCE(NULLIF({alias}.cmd_name, ''), '{_BASH_CMD_UNKNOWN}')"


def _bash_error_rate(errors, measured):
    """Same contract as _outcome_agg(), applied to already-aggregated numbers.

    Used only for rows this module builds by summing other rows (the `other`
    roll-up). NULL in => NULL out: on a DB without the LAV-78 columns errors and
    measured are both NULL ("unknown"), and 0.0 would read as "no errors".
    measured = 0 also yields None, never 0.0 — "not measured" is not "no errors".
    """
    if errors is None or measured is None or measured == 0:
        return None
    return round(1.0 * errors / measured, 4)


def _fold_bash_by_type(rows, limit=_BASH_BY_TYPE_LIMIT):
    """Keep the top `limit` buckets of a by_type result; roll the tail into one row.

    `rows` must already be ordered by count DESC. Returns (rows, folded_count)
    where folded_count is the number of distinct buckets collapsed into the
    trailing `other` row (0 when nothing was folded, in which case no roll-up row
    is appended).

    Ordering contract: the head stays in count-DESC order and the roll-up row,
    when present, is ALWAYS the last element — even when its count outranks the
    head's tail (it usually does). A caller can therefore render the list in
    order and style the final row differently without inspecting the label.

    The roll-up re-derives error_rate from the summed errors/measured — it is NOT
    an average of rates, which would weight a 1-call command the same as a
    5,000-call one.

    If a real command happens to be named `other` (a program on $PATH could be),
    it is merged into the roll-up instead of producing two rows with the same
    label; the alternative is a chart with a duplicated legend entry.
    """
    if len(rows) <= limit:
        return rows, 0

    head, tail = rows[:limit], rows[limit:]
    errors = measured = None
    count = 0
    for r in tail:
        count += r["count"] or 0
        if r.get("errors") is not None:
            errors = (errors or 0) + r["errors"]
        if r.get("measured") is not None:
            measured = (measured or 0) + r["measured"]

    rollup = next((r for r in head if r["cmd_type"] == _BASH_CMD_OTHER), None)
    if rollup is None:
        rollup = {"cmd_type": _BASH_CMD_OTHER, "count": 0,
                  "errors": errors, "measured": measured}
    else:
        head.remove(rollup)  # re-appended last, see the ordering contract above
        if rollup.get("errors") is not None or errors is not None:
            rollup["errors"] = (rollup.get("errors") or 0) + (errors or 0)
        if rollup.get("measured") is not None or measured is not None:
            rollup["measured"] = (rollup.get("measured") or 0) + (measured or 0)

    rollup["count"] = (rollup["count"] or 0) + count
    rollup["error_rate"] = _bash_error_rate(rollup.get("errors"), rollup.get("measured"))
    head.append(rollup)
    return head, len(tail)


def get_bash_stats(conn, project_id=None, user_id=None, host_id=None,
                   start_date=None, end_date=None, client_source=None):
    """Get bash command statistics with 4D filters.

    by_type buckets on bash_commands.cmd_name (LAV-79) — the program the command
    actually runs, so `cd x && npm test` counts as npm and not as 'other'. It is
    truncated to the top _BASH_BY_TYPE_LIMIT buckets plus one `other` roll-up row
    that preserves the total; `by_type_other_count` says how many distinct
    commands that row stands for, and `by_type_source` says which of the two
    bucketing rules produced the list.

    Outcome columns (LAV-78) add `errors`, `measured` and `error_rate` to
    by_type and to totals, plus a `top_errors` list keyed by the same bucket. See
    _outcome_agg() for why the denominator is `measured` and not COUNT(*).
    """
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='bc'
    )
    join = _join_session_sources('bc')

    # Degrade gracefully on a DB that has not been migrated yet.
    bash_cols = _table_columns(conn, 'bash_commands')
    has_outcome = 'is_error' in bash_cols
    outcome_agg = _outcome_agg('bc') if has_outcome else _OUTCOME_AGG_MISSING
    error_text_expr = "bc.error_text" if 'error_text' in bash_cols else "''"
    never_agg = (_never_executed_agg('bc', 'error_text' in bash_cols) if has_outcome
                 else "NULL as never_executed, NULL as errors_unclassified")
    exit_code_expr = "bc.exit_code" if 'exit_code' in bash_cols else "NULL"

    # LAV-79 primary path vs the pre-LAV-79 LIKE ladder. cmd_name is a real
    # column precisely so that PRAGMA table_info (i.e. _table_columns) can see it
    # — a GENERATED column would be invisible here and this guard would report
    # "not migrated" forever.
    has_cmd_name = 'cmd_name' in bash_cols
    cmd_type_expr = _bash_cmd_name_expr('bc') if has_cmd_name else _BASH_CMD_TYPE_CASE

    by_type = run_query(conn, f"""
        SELECT
            {cmd_type_expr} as cmd_type,
            COUNT(*) as count,
            {outcome_agg},
            {never_agg}
        FROM bash_commands bc
        {join}
        {where}
        GROUP BY cmd_type
        ORDER BY count DESC, cmd_type ASC
    """, params if params else None)
    by_type, by_type_other_count = _fold_bash_by_type(by_type)

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
            COUNT(DISTINCT target_file) as unique_files,
            {outcome_agg},
            {never_agg}
        FROM bash_commands bc
        {join}
        {where}
    """, params if params else None)[0]

    # Which commands actually fail, with the exit code they most often fail with.
    # Keyed on the same expression as by_type, so the two can never disagree
    # about what a bucket is. `is_error = 1` already excludes successes (0) and
    # never-measured rows (NULL). exit_code is absent for ~27% of failures
    # (permission-denied / blocked calls never produce one), so top_exit_code is
    # NULL for a bucket whose failures all lack an exit code — it is not a count
    # of anything. No roll-up here: LIMIT 20 on a list sorted by absolute error
    # count is already the question being asked, and an `other` row pooling 280
    # unrelated programs' failures would answer nothing.
    # sample_error_text is a bare column paired with the query's single MAX()
    # aggregate, so SQLite takes it from the most recent failing row
    # (documented behaviour since 3.7.11).
    top_errors = []
    if has_outcome:
        err_where = _and_clause(where, "bc.is_error = 1")
        top_errors = run_query(conn, f"""
            WITH bash_errors AS (
                SELECT
                    {cmd_type_expr} as cmd_type,
                    {exit_code_expr} as exit_code,
                    {error_text_expr} as error_text,
                    bc.timestamp as timestamp
                FROM bash_commands bc
                {join}
                {err_where}
            ),
            exit_code_freq AS (
                SELECT cmd_type, exit_code, COUNT(*) as n
                FROM bash_errors
                WHERE exit_code IS NOT NULL
                GROUP BY cmd_type, exit_code
            )
            SELECT
                e.cmd_type as cmd_type,
                COUNT(*) as errors,
                MAX(e.timestamp) as last_error_at,
                e.error_text as sample_error_text,
                (SELECT f.exit_code FROM exit_code_freq f
                  WHERE f.cmd_type = e.cmd_type
                  ORDER BY f.n DESC, f.exit_code ASC LIMIT 1) as top_exit_code,
                (SELECT f.n FROM exit_code_freq f
                  WHERE f.cmd_type = e.cmd_type
                  ORDER BY f.n DESC, f.exit_code ASC LIMIT 1) as top_exit_code_count
            FROM bash_errors e
            GROUP BY e.cmd_type
            ORDER BY errors DESC, last_error_at DESC
            LIMIT 20
        """, params if params else None)

    return {
        "by_type": by_type,
        "by_type_source": "cmd_name" if has_cmd_name else "command_like",
        "by_type_other_count": by_type_other_count,
        "top_files": top_files,
        "daily": daily,
        "totals": totals,
        "top_errors": top_errors,
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

    so_cols = _table_columns(conn, 'search_operations')
    has_outcome = 'is_error' in so_cols
    outcome_agg = _outcome_agg('so') if has_outcome else _OUTCOME_AGG_MISSING
    never_agg = (_never_executed_agg('so', 'error_text' in so_cols) if has_outcome
                 else "NULL as never_executed, NULL as errors_unclassified")

    by_tool = run_query(conn, f"""
        SELECT tool, COUNT(*) as count,
            {outcome_agg},
            {never_agg}
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
# TOOLS TAB (LAV-85)
# ===========================================================================
#
# Backing queries for /api/tools. Deliberately NOT wired into /api/data: that
# endpoint is awaited before ANY tab paints and is re-fetched on every filter
# change, while this data is needed only when the Tools tab is open. The
# per-tab-endpoint precedent is /api/cost-intelligence and /api/day.

# One row per "family" — the six tool tables, plus builtin_host split out of
# mcp_tool_calls. Order is the display order of the mix chart.
#   family, table, alias, name expression, extra WHERE (or None)
_TOOL_FAMILIES = (
    ("search", "search_operations", "so", "so.tool", None),
    ("mcp", "mcp_tool_calls", "mt",
     "COALESCE(NULLIF(mt.server_name, ''), '(no server)') || '/' || mt.tool_name",
     "COALESCE(mt.kind, '') != 'builtin_host'"),
    ("bash", "bash_commands", "bc", None, None),          # name expr filled at runtime
    ("file", "file_operations", "fo", "fo.tool", None),
    ("builtin_host", "mcp_tool_calls", "mt",
     "COALESCE(NULLIF(mt.server_name, ''), '(no server)') || '/' || mt.tool_name",
     "COALESCE(mt.kind, '') = 'builtin_host'"),
    ("skill", "skill_invocations", "si", "si.skill_name", None),
    ("subagent", "subagent_invocations", "sa", "sa.subagent_type", None),
)

# A leaderboard row needs at least this many MEASURED calls before its rate is
# shown. 3 errors out of 4 calls is not a 75% error rate, it is noise.
DEFAULT_MIN_MEASURED = 10

# Hard cap on leaderboard rows. 604 (family, name) groups exist on the real DB,
# so this never truncates in practice — it is a backstop, and `truncated` says
# so when it fires rather than silently cutting the tail off.
_LEADERBOARD_LIMIT = 300


def _family_legs(conn, filter_kwargs, families=_TOOL_FAMILIES):
    """Per-family (sql_fragment, params, capabilities) for the UNION legs.

    Each family gets its OWN build_filters() call — the alias differs, so the
    WHERE text and the parameter order differ, and the params must be
    concatenated in leg order.
    """
    legs = []
    for family, table, alias, name_expr, extra in families:
        where, params = build_filters(
            project_id=filter_kwargs.get("project_id"),
            user_id=filter_kwargs.get("user_id"),
            host_id=filter_kwargs.get("host_id"),
            start=filter_kwargs.get("start_date"),
            end=filter_kwargs.get("end_date"),
            client=filter_kwargs.get("client_source"),
            table_alias=alias,
        )
        if extra:
            where = _and_clause(where, extra)
        cols = _table_columns(conn, table)
        if name_expr is None:      # bash: cmd_name when migrated, LIKE ladder otherwise
            name_expr = (_bash_cmd_name_expr(alias) if 'cmd_name' in cols
                         else _BASH_CMD_TYPE_CASE)
        legs.append({
            "family": family, "table": table, "alias": alias,
            "name_expr": name_expr, "where": where, "params": params,
            "has_outcome": 'is_error' in cols,
            "has_error_text": 'error_text' in cols,
            "join": _join_session_sources(alias),
        })
    return legs


def _union_all_tools(legs):
    """UNION ALL over every family, normalised to one shape.

    The outcome column is aliased to `is_error` so `_outcome_agg('u')` and
    `_error_class_expr('u')` can be reused VERBATIM on the result — the point of
    giving all six tables the same shape in part B. A family without the columns
    contributes NULLs, which those helpers already read as "not measured".
    """
    parts, params = [], []
    for leg in legs:
        a = leg["alias"]
        is_error = f"{a}.is_error" if leg["has_outcome"] else "NULL"
        error_text = f"{a}.error_text" if leg["has_error_text"] else "NULL"
        parts.append(f"""
            SELECT '{leg['family']}' AS family,
                   {leg['name_expr']} AS name,
                   {is_error} AS is_error,
                   {error_text} AS error_text,
                   {a}.timestamp AS ts
              FROM {leg['table']} {a}
              {leg['join']}
              {leg['where']}""")
        params.extend(leg["params"])
    return " UNION ALL ".join(parts), params


def get_tools_overview(conn, **filter_kwargs):
    """Blocks A + C: global KPIs and the per-family mix, in one pass."""
    legs = _family_legs(conn, filter_kwargs)
    union_sql, params = _union_all_tools(legs)
    # error_text is present on at least one leg, so the taxonomy is meaningful
    # over the union; legs without it contribute NULL and classify as unknown.
    never_agg = _never_executed_agg('u', any(l["has_error_text"] for l in legs))

    mix = run_query(conn, f"""
        WITH u AS ({union_sql})
        SELECT family, COUNT(*) as calls,
            {_outcome_agg('u')},
            {never_agg}
        FROM u GROUP BY family
    """, params if params else None)

    order = {f[0]: i for i, f in enumerate(_TOOL_FAMILIES)}
    mix.sort(key=lambda r: order.get(r["family"], 99))

    totals = run_query(conn, f"""
        WITH u AS ({union_sql})
        SELECT COUNT(*) as calls,
            {_outcome_agg('u')},
            {never_agg}
        FROM u
    """, params if params else None)[0]

    daily = run_query(conn, f"""
        WITH u AS ({union_sql})
        SELECT DATE(ts) as date, COUNT(*) as calls,
               SUM(CASE WHEN is_error = 1 THEN 1 ELSE 0 END) as errors,
               SUM(CASE WHEN is_error IS NOT NULL THEN 1 ELSE 0 END) as measured
        FROM u WHERE ts IS NOT NULL AND ts != ''
        GROUP BY DATE(ts) ORDER BY date
    """, params if params else None)

    measured = totals.get("measured") or 0
    calls = totals.get("calls") or 0
    totals["measured_coverage"] = round(measured / calls, 4) if calls else None
    # The rate a reader actually wants: calls that ran and failed, over calls
    # that ran and were measured. See ERROR_CLASSES_NEVER_EXECUTED.
    never = totals.get("never_executed")
    errors = totals.get("errors")
    if never is not None and errors is not None and measured - never > 0:
        totals["real_error_rate"] = round((errors - never) / (measured - never), 4)
    else:
        totals["real_error_rate"] = None

    return {"totals": totals, "mix": mix, "daily": daily}


def get_tools_leaderboard(conn, min_measured=DEFAULT_MIN_MEASURED, **filter_kwargs):
    """Block B: one ranked table across all six tool tables."""
    legs = _family_legs(conn, filter_kwargs)
    union_sql, params = _union_all_tools(legs)
    never_agg = _never_executed_agg('u', any(l["has_error_text"] for l in legs))

    rows = run_query(conn, f"""
        WITH u AS ({union_sql})
        SELECT family, name, COUNT(*) as calls,
            {_outcome_agg('u')},
            {never_agg},
            MAX(CASE WHEN is_error = 1 THEN ts END) as last_error_at
        FROM u
        GROUP BY family, name
        HAVING measured >= ?
        ORDER BY error_rate DESC, errors DESC, calls DESC
        LIMIT {_LEADERBOARD_LIMIT + 1}
    """, (params + [min_measured]) if params else [min_measured])

    truncated = len(rows) > _LEADERBOARD_LIMIT
    rows = rows[:_LEADERBOARD_LIMIT]
    for r in rows:
        # Precomputed so the client can flip the "count never-executed as
        # errors" toggle without a refetch. NULL in -> NULL out: for a family
        # with no error_text we do not know, and 0 would read as "none".
        ne, err, meas = r.get("never_executed"), r.get("errors"), r.get("measured")
        if ne is None or err is None or meas is None or meas - ne <= 0:
            r["real_error_rate"] = None
            r["errors_executed"] = None if ne is None else max(err - ne, 0)
        else:
            r["errors_executed"] = max(err - ne, 0)
            r["real_error_rate"] = round(r["errors_executed"] / (meas - ne), 4)

    return {"rows": rows, "min_measured": min_measured, "truncated": truncated}


def get_mcp_hierarchy(conn, top_n=10, **filter_kwargs):
    """Block D / MCP: server -> tool, real MCP and built-in host kept apart.

    The two sets are returned separately and must NEVER be summed by the caller:
    built-in host rows carry no outcome data at all (those exports have no
    linkable tool_result), so a combined error rate would be computed over a
    denominator that silently excludes 38% of the calls.
    """
    out = {}
    for key, kind_clause in (("servers", "COALESCE(mt.kind, '') != 'builtin_host'"),
                             ("builtin_host", "COALESCE(mt.kind, '') = 'builtin_host'")):
        where, params = build_filters(
            project_id=filter_kwargs.get("project_id"),
            user_id=filter_kwargs.get("user_id"),
            host_id=filter_kwargs.get("host_id"),
            start=filter_kwargs.get("start_date"),
            end=filter_kwargs.get("end_date"),
            client=filter_kwargs.get("client_source"),
            table_alias='mt',
        )
        where = _and_clause(where, kind_clause)
        join = _join_session_sources('mt')
        cols = _table_columns(conn, 'mcp_tool_calls')
        has_outcome = 'is_error' in cols
        agg = _outcome_agg('mt') if has_outcome else _OUTCOME_AGG_MISSING
        never = (_never_executed_agg('mt', 'error_text' in cols) if has_outcome
                 else "NULL as never_executed, NULL as errors_unclassified")

        servers = run_query(conn, f"""
            SELECT COALESCE(NULLIF(mt.server_name, ''), '(no server)') as server,
                   COUNT(*) as calls, {agg}, {never}
            FROM mcp_tool_calls mt {join} {where}
            GROUP BY server ORDER BY calls DESC
        """, params if params else None)

        tools = run_query(conn, f"""
            SELECT COALESCE(NULLIF(mt.server_name, ''), '(no server)') as server,
                   mt.tool_name as tool, COUNT(*) as calls, {agg}, {never}
            FROM mcp_tool_calls mt {join} {where}
            GROUP BY server, tool ORDER BY calls DESC
        """, params if params else None)

        kept, tail = servers[:top_n], servers[top_n:]
        by_server = {s["server"]: s for s in kept}
        for s in kept:
            s["tools"] = []
        for t in tools:
            parent = by_server.get(t["server"])
            if parent is not None:
                parent["tools"].append(t)

        other = None
        if tail:
            # Re-derive the rate from summed errors/measured — never average
            # rates. Same rule as _fold_bash_by_type's roll-up row.
            errs = sum((s.get("errors") or 0) for s in tail)
            meas = sum((s.get("measured") or 0) for s in tail)
            other = {
                "server_count": len(tail),
                "calls": sum(s["calls"] for s in tail),
                "errors": errs, "measured": meas,
                "error_rate": _bash_error_rate(errs, meas),
            }
        out[key] = {"servers": kept, "other": other}
    return out


def get_bash_drilldown(conn, cmd_name, **filter_kwargs):
    """Bash level 2: why one command fails, not just how often."""
    where, params = build_filters(
        project_id=filter_kwargs.get("project_id"),
        user_id=filter_kwargs.get("user_id"),
        host_id=filter_kwargs.get("host_id"),
        start=filter_kwargs.get("start_date"),
        end=filter_kwargs.get("end_date"),
        client=filter_kwargs.get("client_source"),
        table_alias='bc',
    )
    join = _join_session_sources('bc')
    cols = _table_columns(conn, 'bash_commands')
    has_cmd_name = 'cmd_name' in cols
    has_outcome = 'is_error' in cols
    has_error_text = 'error_text' in cols
    name_expr = _bash_cmd_name_expr('bc') if has_cmd_name else _BASH_CMD_TYPE_CASE
    where = _and_clause(where, f"{name_expr} = ?")
    params = list(params) + [cmd_name]

    agg = _outcome_agg('bc') if has_outcome else _OUTCOME_AGG_MISSING
    never = (_never_executed_agg('bc', has_error_text) if has_outcome
             else "NULL as never_executed, NULL as errors_unclassified")

    totals = run_query(conn, f"""
        SELECT COUNT(*) as calls, {agg}, {never}
        FROM bash_commands bc {join} {where}
    """, params)[0]

    err_where = _and_clause(where, "bc.is_error = 1") if has_outcome else where
    classes = run_query(conn, f"""
        SELECT {_error_class_expr('bc', has_error_text)} as class, COUNT(*) as n
        FROM bash_commands bc {join} {err_where}
        GROUP BY class ORDER BY n DESC
    """, params) if has_outcome else []

    exit_codes = run_query(conn, f"""
        SELECT bc.exit_code as exit_code, COUNT(*) as n
        FROM bash_commands bc {join} {err_where}
        GROUP BY bc.exit_code ORDER BY n DESC
    """, params) if 'exit_code' in cols else []

    # substr IN SQL, not in Python: a heredoc command can be tens of KB and
    # there is no reason to ship it just to truncate it client-side.
    variants = run_query(conn, f"""
        SELECT substr(bc.command, 1, 160) as command, COUNT(*) as n,
               SUM(CASE WHEN bc.is_error = 1 THEN 1 ELSE 0 END) as errors
        FROM bash_commands bc {join} {where}
        GROUP BY substr(bc.command, 1, 160)
        ORDER BY n DESC LIMIT 15
    """, params) if has_outcome else []

    recent = run_query(conn, f"""
        SELECT bc.timestamp as timestamp, bc.session_id as session_id, p.name as project,
               bc.exit_code as exit_code,
               substr(COALESCE(bc.error_text, ''), 1, 400) as error_text,
               substr(bc.command, 1, 200) as command
        FROM bash_commands bc
        JOIN projects p ON p.id = bc.project_id
        {join} {err_where}
        ORDER BY bc.timestamp DESC LIMIT 20
    """, params) if has_outcome else []

    return {"cmd_name": cmd_name, "totals": totals, "error_classes": classes,
            "exit_codes": exit_codes, "variants": variants, "recent_failures": recent}


def get_tool_error_samples(conn, family, name, **filter_kwargs):
    """Side panel: what a tool actually says when it fails."""
    leg = next((f for f in _TOOL_FAMILIES if f[0] == family), None)
    if leg is None:
        return {"family": family, "name": name, "has_error_text": False,
                "top_texts": [], "recent": []}
    _f, table, alias, name_expr, extra = leg
    where, params = build_filters(
        project_id=filter_kwargs.get("project_id"),
        user_id=filter_kwargs.get("user_id"),
        host_id=filter_kwargs.get("host_id"),
        start=filter_kwargs.get("start_date"),
        end=filter_kwargs.get("end_date"),
        client=filter_kwargs.get("client_source"),
        table_alias=alias,
    )
    if extra:
        where = _and_clause(where, extra)
    cols = _table_columns(conn, table)
    if name_expr is None:
        name_expr = (_bash_cmd_name_expr(alias) if 'cmd_name' in cols
                     else _BASH_CMD_TYPE_CASE)
    where = _and_clause(where, f"{name_expr} = ?")
    params = list(params) + [name]
    if 'is_error' not in cols:
        return {"family": family, "name": name, "has_error_text": False,
                "top_texts": [], "recent": []}
    where = _and_clause(where, f"{alias}.is_error = 1")
    join = _join_session_sources(alias)
    has_error_text = 'error_text' in cols

    top_texts = run_query(conn, f"""
        SELECT substr({alias}.error_text, 1, 200) as text, COUNT(*) as n,
               MAX({alias}.timestamp) as last_at,
               {_error_class_expr(alias, True)} as class
        FROM {table} {alias} {join} {where}
          AND COALESCE({alias}.error_text, '') != ''
        GROUP BY substr({alias}.error_text, 1, 200)
        ORDER BY n DESC LIMIT 5
    """, params) if has_error_text else []

    recent = run_query(conn, f"""
        SELECT {alias}.timestamp as timestamp, {alias}.session_id as session_id,
               p.name as project
        FROM {table} {alias}
        JOIN projects p ON p.id = {alias}.project_id
        {join} {where}
        ORDER BY {alias}.timestamp DESC LIMIT 20
    """, params)

    return {"family": family, "name": name, "has_error_text": has_error_text,
            "top_texts": top_texts, "recent": recent}


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

# A row is a child (hidden from the grouped list, rolled up under its master) iff its
# parent_session_id is set, differs from its own session_id (SELF-PARENT GUARD — some
# masters point to themselves), AND a row with that parent session_id exists ANYWHERE.
# NB: the parent lookup is intentionally project-AGNOSTIC. A Cowork conversation's
# master lives in cowork_default while its slaves get scattered into per-event
# inferred projects (outputs/miniMe/...), so a same-project EXISTS would leak those
# slaves back into the list. session_id is a globally-unique UUID, so matching on
# session_id alone is safe for both Cowork (cross-project tree) and Claude Code
# (same-project subagents).
# Otherwise the row is top-level: this covers real masters (NULL parent), self-parent
# masters, and ORPHAN PROMOTION (parent does not resolve to any existing master, so
# nothing disappears from the list).
# The list filters on it and the cost profile of a session page reads it: a session
# that is a master in one and a child in the other would show two totals.
_TOP_LEVEL_SQL = """(c.parent_session_id IS NULL
            OR c.parent_session_id = c.session_id
            OR NOT EXISTS (
                SELECT 1 FROM interactions m
                WHERE m.session_id = c.parent_session_id
            ))"""


def get_interactions_list(conn, project_id=None, user_id=None, host_id=None,
                           search=None, start_date=None, end_date=None,
                           limit=50, offset=0, client_source=None,
                           classification=None, sensitivity=None, grouped=True):
    """Get list of interactions with 4D filters + optional metadata filters.

    When grouped=True (default) only top-level masters (parent_session_id IS NULL)
    are returned, with cost_usd/total_tokens rolled up over the session and all its
    transitive descendants (via parent_session_id), plus derived_count. Orphan rows
    whose parent does not resolve to a master are promoted to top-level.

    When grouped=False, behavior is exactly as before: every session is a row, with
    per-session cost, no parent filter, and derived_count is 0.
    """
    where, params = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=start_date, end=end_date, client=client_source, table_alias='c'
    )
    join = _join_session_sources('c')

    if search:
        # If search looks like a UUID/session_id, match directly and drop date
        # filters so the result is always found regardless of date range.
        # LAV-66: in grouped mode child sessions (subagents, parent_session_id set)
        # are hidden from the list, so a match that lives only in a child (e.g. a
        # term appearing only in a subagent transcript, or a pasted child session
        # id) would return nothing. Also match masters that have a matching child.
        import re
        if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-', search, re.IGNORECASE):
            # Rebuild filters without date constraints
            where, params = build_filters(
                project_id=project_id, user_id=user_id, host_id=host_id,
                client=client_source, table_alias='c'
            )
            if grouped:
                extra = """(c.session_id = ? OR EXISTS (
                    SELECT 1 FROM interactions ch
                    WHERE ch.parent_session_id = c.session_id AND ch.session_id = ?
                ))"""
                search_params = [search, search]
            else:
                extra = "c.session_id = ?"
                search_params = [search]
        else:
            fts_subquery = """SELECT DISTINCT m.session_id FROM messages m
                JOIN messages_fts ON messages_fts.rowid = m.id
                WHERE messages_fts MATCH ?"""
            if grouped:
                extra = f"""(c.session_id IN ({fts_subquery})
                    OR c.session_id IN (
                        SELECT ch.parent_session_id FROM interactions ch
                        WHERE ch.parent_session_id IS NOT NULL
                          AND ch.session_id IN ({fts_subquery})
                    ))"""
                search_params = [search, search]
            else:
                extra = f"c.session_id IN ({fts_subquery})"
                search_params = [search]
        if where:
            where += " AND " + extra
        else:
            where = " WHERE " + extra
        params.extend(search_params)

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

    # Grouped mode: only show top-level masters. Applied to BOTH the SELECT and the
    # COUNT WHERE so pagination/total stay consistent. The descendant rollup happens
    # in Python below (correct for arbitrary nesting depth), which also fills cost_usd.
    if grouped:
        extra = _TOP_LEVEL_SQL
        if where:
            where += " AND " + extra
        else:
            where = " WHERE " + extra

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
            {"0" if grouped else _per_session_cost_subquery()} as cost_usd
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

    if grouped:
        _rollup_descendants(conn, interactions)
    else:
        for row in interactions:
            row['derived_count'] = 0

    return {
        "interactions": interactions,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _descendant_closure(conn, roots):
    """All the transitive descendants of each root: {root: {(session_id, project_id): row}}.

    The one walk of the session tree: the grouped list and the cost profile of a session
    page both go through here, or they would diverge again. `roots` are (session_id,
    project_id) pairs; each row has session_id, project_id, parent_session_id and
    total_tokens.

    Breadth-first via parent_session_id, so it is correct for arbitrary nesting depth
    (Claude Code nests; Cowork is one level — same code path).

    The walk is project-AGNOSTIC on the parent link: a Cowork conversation's master
    sits in cowork_default while its slaves get scattered into per-event inferred
    projects, so descendants can live in a different project than their master. We
    still key each node on (session_id, project_id) — the same session_id can be
    materialised under multiple projects (composite PK), and each such copy carries
    its own tokens and must be counted exactly once per root. It follows that two
    copies of a root have the same descendants.
    """
    children_of = {}    # parent session_id -> its child rows
    expanded = set()    # session_ids already asked for: a cycle stops here
    frontier = {session_id for session_id, _ in roots}
    while frontier:
        expanded |= frontier
        next_frontier = set()
        for session_ids in _in_chunks(frontier):
            placeholders = ",".join("?" for _ in session_ids)
            children = run_query(conn, f"""
                SELECT
                    c.session_id,
                    c.project_id,
                    c.parent_session_id,
                    COALESCE(c.total_tokens, 0) as total_tokens
                FROM interactions c
                WHERE c.parent_session_id IN ({placeholders})
                  AND c.session_id != c.parent_session_id
            """, session_ids)
            for child in children:
                children_of.setdefault(child['parent_session_id'], []).append(child)
                if child['session_id'] not in expanded:
                    next_frontier.add(child['session_id'])
        frontier = next_frontier

    closure = {}
    for root in roots:
        descendants = {}
        to_expand = [root[0]]
        seen = {root[0]}
        while to_expand:
            for child in children_of.get(to_expand.pop(), ()):
                key = (child['session_id'], child['project_id'])
                if key == root or key in descendants:
                    # Cycle guard / already-counted node.
                    continue
                descendants[key] = child
                if child['session_id'] not in seen:
                    seen.add(child['session_id'])
                    to_expand.append(child['session_id'])
        closure[root] = descendants
    return closure


def _rollup_descendants(conn, masters):
    """Roll up cost_usd/total_tokens of all transitive descendants onto each master.

    Each master gains:
      - cost_usd = own cost + all descendants, added unrounded and rounded once at
        the end, so that it is the total of the session's page
      - total_tokens = own value + sum over all descendants
      - derived_count = number of descendants

    Masters are mutated in place.
    """
    if not masters:
        return

    roots = [(m['session_id'], m['project_id']) for m in masters]
    closure = _descendant_closure(conn, roots)
    costs = _sessions_cost(conn, set(roots).union(*closure.values()))

    for master, root in zip(masters, roots):
        descendants = closure[root]
        master['cost_usd'] = _round_cost(sum(costs.get(key, 0) for key in (root, *descendants)))
        master['total_tokens'] = (master.get('total_tokens') or 0) + sum(
            child['total_tokens'] for child in descendants.values())
        master['derived_count'] = len(descendants)


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


def get_interaction_children(conn, session_id, project_id=None):
    """Get direct child (derived) sessions of a master, with per-session cost.

    One level deep, ordered oldest-first. Reuses the per-session cost subquery
    shape. Returns a list of dicts with keys: session_id, project_id, summary,
    display, agent_id, timestamp, total_tokens, message_count, cost_usd,
    meta_summary (AI classification summary, may be NULL), tools_used (JSON
    string of tool-name list, may be NULL).

    Matching is project-AGNOSTIC on the parent link: a Cowork master sits in
    cowork_default while its slaves land in per-event inferred projects, so the
    children of a master can live in different projects. session_id is a globally
    unique UUID, so matching on parent_session_id alone is safe. The project_id
    argument is accepted for call-site compatibility but intentionally not used as
    a filter.

    meta_summary + tools_used feed the enriched derived-sessions list and the
    inline subagent markers in the transcript (LAV-67). Both are additive and may
    be NULL for unclassified children; callers read them defensively.
    """
    children = run_query(conn, """
        SELECT
            c.session_id,
            c.project_id,
            c.summary,
            c.display,
            c.agent_id,
            c.timestamp,
            c.total_tokens,
            c.message_count,
            c.tools_used,
            cm.summary as meta_summary
        FROM interactions c
        LEFT JOIN interaction_metadata cm
            ON cm.session_id = c.session_id AND cm.project_id = c.project_id
        WHERE c.parent_session_id = ?
        ORDER BY c.timestamp ASC
    """, [session_id])
    costs = _sessions_cost(
        conn, [(child['session_id'], child['project_id']) for child in children], rounded=True)
    for child in children:
        child['cost_usd'] = costs.get((child['session_id'], child['project_id']), 0)
    return children


def get_parent_interaction(conn, interaction):
    """The parent row for the link on a child session's page, None if it does not exist.

    get_interaction_detail looks for the parent in the child's own project, so a child
    scattered in another project gets none. Here the link is project-AGNOSTIC like the
    rest of the tree, and project_id says which copy of the parent to open: the one in
    the child's project when there is one.
    """
    parent = run_query(conn, """
        SELECT session_id, project_id, summary, display, timestamp
        FROM interactions
        WHERE session_id = ?
        ORDER BY project_id = ? DESC, project_id ASC
        LIMIT 1
    """, [interaction['parent_session_id'], interaction['project_id']])
    return parent[0] if parent else None


def get_interaction_detail(conn, session_id, project_id=None):
    """Get full interaction transcript with parent info."""
    cost_subquery = f"{_per_session_cost_subquery()} as cost_usd"
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

    interaction['children'] = get_interaction_children(conn, session_id, pid)

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
        HAVING COUNT(DISTINCT c.session_id) > 0
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

def _is_child_session(conn, session_id, project_id):
    """True if the grouped list hides this session under a master.

    Without project_id every copy of the session must be a child. A session with no
    interactions row is not one.
    """
    where = "c.session_id = ?"
    params = [session_id]
    if project_id is not None:
        where += " AND c.project_id = ?"
        params.append(project_id)
    row = run_query(conn, f"""
        SELECT MAX({_TOP_LEVEL_SQL}) as top_level
        FROM interactions c
        WHERE {where}
    """, params)
    return row[0]['top_level'] == 0


def _priced_token_rows(conn, session_id, project_id, descendants):
    """Unrounded token_usage rows of a session and its descendants, in time order.

    One scan for all of them, of the projects involved: a session without project_id
    takes all its copies, so there the filter is on session_id only. A row of another
    copy of a session that is not a node of the tree comes back too: the caller drops it.
    """
    project_ids = []
    project_clause = ""
    if project_id is not None:
        project_ids = sorted({project_id, *(key[1] for key in descendants)})
        project_clause = f"tu.project_id IN ({','.join('?' for _ in project_ids)}) AND"

    rows = []
    for session_ids in _in_chunks({session_id, *(key[0] for key in descendants)}):
        rows += run_query(conn, f"""
            SELECT
                tu.id,
                tu.session_id,
                tu.project_id,
                tu.timestamp,
                tu.model,
                COALESCE(tu.input_tokens, 0) as input_tokens,
                COALESCE(tu.output_tokens, 0) as output_tokens,
                COALESCE(tu.cache_creation_tokens, 0) as cache_creation_tokens,
                COALESCE(tu.cache_read_tokens, 0) as cache_read_tokens,
                COALESCE(tu.api_message_id, '') as api_message_id,
                {_COST.total} as cost_usd,
                {_COST.input} as cost_input,
                {_COST.output} as cost_output,
                {_COST.cache_write} as cost_cache_write,
                {_COST.cache_read} as cost_cache_read
            FROM token_usage tu
            {_PRICE_JOIN}
            WHERE {project_clause} tu.session_id IN ({",".join("?" for _ in session_ids)})
        """, project_ids + session_ids)
    rows.sort(key=lambda r: (r["timestamp"], r["id"]))
    return rows


_AGENT_LABEL_MAX_CHARS = 40
_AGENT_LABEL_PROMPT_PREFIX = 80


def _short_label(text):
    """One line, cut to fit a table column."""
    text = " ".join(text.split())
    if len(text) <= _AGENT_LABEL_MAX_CHARS:
        return text
    return text[:_AGENT_LABEL_MAX_CHARS].rstrip() + "…"


def _child_session_agents(conn, keys):
    """agent_id and a short human label of child sessions: {(session_id, project_id): {...}}.

    The label is the description the subagent was launched with, else its type, else
    its summary or first message, else the start of its id. Nothing links a child to
    the subagent_invocations row of its parent, but the child's first message
    (`display`, cut at 200 chars) is the invocation's prompt, and invocations with the
    same prompt are told apart by order of time. No match (workflow children, old
    Warmup sessions) falls to the summary.
    """
    children = []
    for session_ids in _in_chunks({session_id for session_id, _ in keys}):
        children += run_query(conn, f"""
            SELECT c.session_id, c.project_id, c.parent_session_id, c.agent_id,
                   c.summary, c.display, c.timestamp
            FROM interactions c
            WHERE c.session_id IN ({",".join("?" for _ in session_ids)})
        """, session_ids)
    children.sort(key=lambda child: child['timestamp'] or '')

    invocations = {}    # parent session_id -> invocations not paired yet, oldest first
    parents = {child['parent_session_id'] for child in children if child['parent_session_id']}
    for session_ids in _in_chunks(parents):
        rows = run_query(conn, f"""
            SELECT session_id, subagent_type, description,
                   SUBSTR(prompt, 1, {_AGENT_LABEL_PROMPT_PREFIX}) as prompt
            FROM subagent_invocations
            WHERE session_id IN ({",".join("?" for _ in session_ids)})
            ORDER BY timestamp ASC, id ASC
        """, session_ids)
        for row in rows:
            invocations.setdefault(row['session_id'], []).append(row)

    agents = {}
    for child in children:
        key = (child['session_id'], child['project_id'])
        if key not in keys:
            continue
        prompt = (child['display'] or '')[:_AGENT_LABEL_PROMPT_PREFIX]
        candidates = invocations.get(child['parent_session_id'], [])
        invocation = next((i for i in candidates if prompt and (i['prompt'] or '').startswith(prompt)), {})
        if invocation:
            candidates.remove(invocation)
        subagent_type = invocation.get('subagent_type')
        summary = child['summary']
        label = (invocation.get('description')
                 or (subagent_type if subagent_type != '(unspecified)' else None)
                 or (summary if summary != '(no title)' else None)
                 or child['display']
                 or (child['agent_id'] or child['session_id'])[:8])
        agents[key] = {"agent_id": child['agent_id'], "label": _short_label(label)}
    return agents


_COST_GROUP_TOKENS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")
_COST_GROUP_COSTS = ("cost_usd", "cost_input", "cost_output", "cost_cache_write", "cost_cache_read")


def _new_cost_group():
    return {"api_calls": 0, "models": set(), **dict.fromkeys(_COST_GROUP_TOKENS, 0),
            **dict.fromkeys(_COST_GROUP_COSTS, 0.0)}


def _add_to_cost_group(group, row):
    group["api_calls"] += 1
    if row["model"]:
        group["models"].add(row["model"])
    for field in _COST_GROUP_TOKENS + _COST_GROUP_COSTS:
        group[field] += row[field]


def _cost_group_output(group):
    """A cost group as the API shows it: its costs are rounded here and only here."""
    return {
        "cost_usd": _round_cost(group["cost_usd"]),
        "api_calls": group["api_calls"],
        "models": sorted(group["models"]),
        "tokens": {
            "input": group["input_tokens"],
            "output": group["output_tokens"],
            "cache_creation": group["cache_creation_tokens"],
            "cache_read": group["cache_read_tokens"],
            "total": sum(group[field] for field in _COST_GROUP_TOKENS),
        },
        "cost": {
            "input": _round_cost(group["cost_input"]),
            "output": _round_cost(group["cost_output"]),
            "cache_write": _round_cost(group["cost_cache_write"]),
            "cache_read": _round_cost(group["cost_cache_read"]),
            "total": _round_cost(group["cost_usd"]),
        },
    }


def get_session_cost_profile(conn, session_id, project_id=None):
    """Cost of a session's page: totals, split main/subagents, timeline and phases.

    A master counts its own rows and those of its whole descendant closure, the walk
    and the rounding of the grouped list, so list and page show one total. A child
    session (the list's rule: _TOP_LEVEL_SQL) counts only its own rows and is_subagent
    is true, even if it has children of its own. None when there is no row at all.

    total, own, subagents: {cost_usd, api_calls, models, tokens, cost}; subagents also
    has count, the descendant sessions including those with no token rows.
    duration_minutes is from the main session's rows only: subagents run in parallel,
    adding them would falsify it. Each timeline row says whose it is (session_id,
    agent_id, is_subagent, agent_label) and where the running total stands.
    """
    is_subagent = _is_child_session(conn, session_id, project_id)
    root = (session_id, project_id)
    descendants = {} if is_subagent else _descendant_closure(conn, [root])[root]

    def is_own(row):
        return row["session_id"] == session_id and project_id in (None, row["project_id"])

    rows = [r for r in _priced_token_rows(conn, session_id, project_id, descendants)
            if is_own(r) or (r["session_id"], r["project_id"]) in descendants]
    if not rows:
        return None

    # On a child's page every row is that subagent's.
    agents = _child_session_agents(conn, {
        (r["session_id"], r["project_id"]) for r in rows if is_subagent or not is_own(r)})

    total = _new_cost_group()
    own = _new_cost_group()
    subagents = _new_cost_group()
    own_rows = []
    timeline = []
    for r in rows:
        _add_to_cost_group(total, r)
        if is_own(r):
            _add_to_cost_group(own, r)
            own_rows.append(r)
        else:
            _add_to_cost_group(subagents, r)
        agent = agents.get((r["session_id"], r["project_id"])) or {}
        timeline.append({
            "timestamp": r["timestamp"],
            "model": r["model"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cache_creation_tokens": r["cache_creation_tokens"],
            "cache_read_tokens": r["cache_read_tokens"],
            "api_message_id": r["api_message_id"],
            "session_id": r["session_id"],
            "agent_id": agent.get("agent_id"),
            "is_subagent": is_subagent or not is_own(r),
            "agent_label": agent.get("label"),
            "cost_usd": round(r["cost_usd"], 6),
            "cumulative_cost": _round_cost(total["cost_usd"]),
            "cumulative_own": _round_cost(own["cost_usd"]),
            "cumulative_subagents": _round_cost(subagents["cost_usd"]),
        })

    # Duration
    first_ts = own_rows[0]["timestamp"] if own_rows else ""
    last_ts = own_rows[-1]["timestamp"] if own_rows else ""
    duration_minutes = 0
    if first_ts and last_ts and len(own_rows) > 1:
        from datetime import datetime
        try:
            fmt = "%Y-%m-%dT%H:%M:%S" if "T" in first_ts else "%Y-%m-%d %H:%M:%S"
            t0 = datetime.strptime(first_ts[:19], fmt)
            t1 = datetime.strptime(last_ts[:19], fmt)
            duration_minutes = round((t1 - t0).total_seconds() / 60, 1)
        except (ValueError, TypeError):
            pass

    # Phases: split into thirds
    n = len(rows)
    third = max(n // 3, 1)
    slices = [rows[:third], rows[third:2*third], rows[2*third:]]
    phase_names = ["early", "mid", "late"]
    phases = {}
    for name, sl in zip(phase_names, slices):
        phase_cost = sum(r["cost_usd"] for r in sl)
        phases[name] = {
            "cost": _round_cost(phase_cost),
            "pct": round(phase_cost / total["cost_usd"] * 100, 1) if total["cost_usd"] > 0 else 0,
        }

    return {
        "total": _cost_group_output(total),
        "own": _cost_group_output(own),
        "subagents": {**_cost_group_output(subagents), "count": len(descendants)},
        "duration_minutes": duration_minutes,
        "is_subagent": is_subagent,
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
                ROUND(SUM({_COST.total}), 4) as session_cost,
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
                    {_per_session_cost_subquery(tu="tu2", mp="mp2")} as session_cost
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
                    {_per_session_cost_subquery()} as session_cost,
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
            ROUND(SUM({_COST.total}) / NULLIF(COUNT(*), 0), 4) as avg_cost_per_call
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
                ROUND(SUM({_COST.total}), 4) as session_cost,
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
                ROUND(SUM({_COST.total}), 4) as session_cost,
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


# The child tables that travel from an agent to the collector. The export ships
# them and the reconcile manifest (LAV-98) compares them, so a table added here is
# covered by both.
EXPORT_CHILD_TABLES = (
    "messages",
    "token_usage",
    "file_operations",
    "bash_commands",
    "search_operations",
    "skill_invocations",
    "subagent_invocations",
    "mcp_tool_calls",
)

_EXPORT_INTERACTION_SQL = """
    SELECT c.*, p.name as project_name, u.username, h.hostname, h.os_type,
           COALESCE(ss.source, 'unknown') as client_source
    FROM interactions c
    LEFT JOIN projects p ON p.id = c.project_id
    LEFT JOIN users u ON u.id = c.user_id
    LEFT JOIN hosts h ON h.id = c.host_id
    LEFT JOIN session_sources ss ON ss.session_id = c.session_id AND ss.project_id = c.project_id
"""

_SESSION_ROW_SQL = """
    SELECT c.session_id, c.project_id, c.timestamp, c.project, p.name AS project_name
    FROM interactions c
    LEFT JOIN projects p ON p.id = c.project_id
"""

# Keys per query: each (session_id, project_id) pair is two SQL variables and SQLite
# allows 32766, so a reconcile over the whole history must be split.
_SESSION_KEY_CHUNK = 5000


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _export_project_name(row: dict) -> str:
    """The project name ingest_remote_sessions() will resolve for this row.

    Project ids differ between nodes, so (session_id, this name) is the identity of
    a session across them: the manifest and the targeted export must agree with
    the ingest, including the fallbacks.
    """
    return row.get("project_name") or row.get("project") or "unknown"


def _session_rows_by_id(conn, session_ids) -> list:
    """Interaction rows (id, project, timestamp only) of these session ids, in any project."""
    rows = []
    for chunk in _chunks(sorted(set(session_ids)), _SESSION_KEY_CHUNK):
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(run_query(
            conn, f"{_SESSION_ROW_SQL} WHERE c.session_id IN ({placeholders})", chunk))
    return rows


def session_projects(conn, session_ids) -> dict:
    """{session_id: [{project_id, project_name}, ...]} for the sessions present here."""
    found = {}
    for row in _session_rows_by_id(conn, session_ids):
        found.setdefault(row["session_id"], []).append(
            {"project_id": row["project_id"], "project_name": _export_project_name(row)})
    return found


def session_manifest(conn, since=None, until=None, keys=None) -> list:
    """Per-session row counts, to compare two nodes without shipping the rows (LAV-98).

    Sessions come from `keys` ((session_id, project_name) pairs) when given, otherwise
    from last activity > `since` (no `since`: all of them). The rule is the one in
    export_sessions(), COALESCE(MAX(messages.timestamp), c.timestamp) > since, written
    so it can use idx_msg_timestamp: a session has a last message newer than `since`
    exactly when it has ANY message newer than `since`. MAX over the whole table has to
    read each row past its content and takes 20 s on a 10 GB DB; this takes under 1 s.

    Returns [{session_id, project_name, last_activity, tables: {table: {count, max_ts}}}].
    `last_activity` always looks at every message. The counts cover the WHOLE session,
    not just the window, and with `until` only rows with timestamp <= until: rows the
    parser is still writing would make every live session look different. A table with
    no such rows is omitted.

    Read-only on purpose (no temp tables): the agent serves it under PRAGMA query_only=ON.
    """
    if keys is not None:
        wanted = {tuple(key) for key in keys}
        selected = [r for r in _session_rows_by_id(conn, [key[0] for key in wanted])
                    if (r["session_id"], _export_project_name(r)) in wanted]
    elif since is not None:
        selected = run_query(conn, f"""
            {_SESSION_ROW_SQL}
            WHERE (c.session_id, c.project_id) IN (
                      SELECT session_id, project_id FROM messages WHERE timestamp > ?)
               OR (c.timestamp > ? AND NOT EXISTS (
                      SELECT 1 FROM messages m
                      WHERE m.session_id = c.session_id AND m.project_id = c.project_id
                        AND m.timestamp IS NOT NULL))
        """, [since, since])
    else:
        selected = run_query(conn, _SESSION_ROW_SQL)

    counted = "timestamp <= ?" if until is not None else "1"
    counted_params = [until, until] if until is not None else []

    tables = {}
    newest_message = {}
    session_keys = [(r["session_id"], r["project_id"]) for r in selected]
    for chunk in _chunks(session_keys, _SESSION_KEY_CHUNK):
        placeholders = ",".join(["(?,?)" for _ in chunk])
        flat_params = [value for key in chunk for value in key]
        for table in EXPORT_CHILD_TABLES:
            rows = conn.execute(f"""
                SELECT session_id, project_id, SUM({counted}),
                       MAX(CASE WHEN {counted} THEN timestamp END), MAX(timestamp)
                FROM {table}
                WHERE (session_id, project_id) IN ({placeholders})
                GROUP BY session_id, project_id
            """, counted_params + flat_params)
            for session_id, project_id, count, max_ts, newest in rows:
                key = (session_id, project_id)
                if count:
                    tables.setdefault(key, {})[table] = {"count": count, "max_ts": max_ts}
                if table == "messages":
                    newest_message[key] = newest

    manifest = []
    for row in selected:
        key = (row["session_id"], row["project_id"])
        newest = newest_message.get(key)
        manifest.append({
            "session_id": row["session_id"],
            "project_name": _export_project_name(row),
            "last_activity": newest if newest is not None else row["timestamp"],
            "tables": tables.get(key, {}),
        })
    manifest.sort(key=lambda s: s["last_activity"] or "")
    return manifest


def _assemble_export(conn, interactions: list, since_ts: str = None) -> list:
    """Attach the child rows to `interactions`, the shape ingest_remote_sessions() reads.

    With `since_ts`, only child rows newer than it. Batch-loads all child tables to
    avoid N+1 queries.
    """
    if not interactions:
        return []

    # Collect all (session_id, project_id) pairs for batch loading
    session_keys = [(c["session_id"], c["project_id"]) for c in interactions]

    # Build IN clause for batch queries
    placeholders = ",".join(["(?,?)" for _ in session_keys])
    flat_params = []
    for sid, pid in session_keys:
        flat_params.extend([sid, pid])

    time_filter = "AND timestamp > ?" if since_ts is not None else ""
    time_params = [since_ts] if since_ts is not None else []

    def batch_load(table):
        rows = run_query(conn, f"""
            SELECT * FROM {table}
            WHERE (session_id, project_id) IN ({placeholders})
              {time_filter}
        """, flat_params + time_params)
        grouped = {}
        for r in rows:
            key = (r["session_id"], r["project_id"])
            grouped.setdefault(key, []).append(r)
        return grouped

    child_maps = {table: batch_load(table) for table in EXPORT_CHILD_TABLES}

    # Assemble sessions
    sessions = []
    for conv in interactions:
        key = (conv["session_id"], conv["project_id"])
        session = {"interaction": conv}
        for table in EXPORT_CHILD_TABLES:
            session[table] = child_maps[table].get(key, [])
        sessions.append(session)

    return sessions


def export_sessions(conn, since_ts: str, limit: int = 1000) -> list:
    """Export sessions with all related data since a timestamp.

    `since_ts` is compared against per-session activity (max message timestamp,
    falling back to interaction.timestamp), so long-running "river" sessions
    keep being exported as new messages append. Child tables are also filtered
    to `timestamp > since_ts` so a re-exported session only carries the rows
    that arrived after the collector's last cursor.

    Returns list of session dicts with nested data.
    """
    # Select sessions whose last-activity timestamp is newer than `since_ts`.
    # `last_activity_ts` is COALESCE(MAX(messages.timestamp), c.timestamp) so
    # ghost sessions (no messages) still surface once via their interaction ts.
    interactions = run_query(conn, f"""
        {_EXPORT_INTERACTION_SQL}
        LEFT JOIN (
            SELECT session_id, project_id, MAX(timestamp) AS last_msg_ts
            FROM messages
            GROUP BY session_id, project_id
        ) m ON m.session_id = c.session_id AND m.project_id = c.project_id
        WHERE COALESCE(m.last_msg_ts, c.timestamp) > ?
        ORDER BY COALESCE(m.last_msg_ts, c.timestamp) ASC
        LIMIT ?
    """, [since_ts, limit])

    # Child rows restricted to rows newer than `since_ts` so a re-exported river
    # session ships only its new tail.
    return _assemble_export(conn, interactions, since_ts)


def export_session_keys(conn, keys) -> list:
    """Export these sessions whole: `keys` are (session_id, project_name) pairs (LAV-98).

    Same shape as export_sessions(), with NO timestamp filter on the child rows: the
    reconcile asks for a session because the cursor lost some of its rows, which by
    definition are older than any cursor. Inserts on the other side are guarded, so
    shipping the rows it already holds costs bandwidth and nothing else.
    """
    wanted = {tuple(key) for key in keys}
    interactions = []
    for chunk in _chunks(sorted({key[0] for key in wanted}), _SESSION_KEY_CHUNK):
        placeholders = ",".join("?" for _ in chunk)
        rows = run_query(
            conn, f"{_EXPORT_INTERACTION_SQL} WHERE c.session_id IN ({placeholders})", chunk)
        interactions.extend(r for r in rows
                            if (r["session_id"], _export_project_name(r)) in wanted)
    return _assemble_export(conn, interactions)


# ===========================================================================
# DAY VIEW / WORKTIME
# ===========================================================================

def get_day_bundle(conn, date_str: str,
                   project_id=None, user_id=None, host_id=None,
                   client_source=None,
                   active_gap_sec: int = 300,
                   assistant_cap_sec: int = 600) -> dict:
    """Day View bundle: per-session Gantt rows + concurrency curve + honest worktime metrics.

    For sessions active on `date_str` (UTC date string YYYY-MM-DD), aggregates
    only the messages that fall on that day — so a "river" session straddling
    midnight surfaces on both days with the local slice on each.

    Honest metrics:
      - active_wallclock_sec: union of per-session activity windows (gap <
        `active_gap_sec` between consecutive messages counts as continuous),
        deduplicated across parallel sessions.
      - assistant_wallclock_sec: union of (user → next assistant) intervals
        capped at `assistant_cap_sec`, deduplicated.

    Subagents are counted (meta.subagent_invocations) but not drawn as
    separate Gantt rows in v1 — they're parented to the parent session.
    """
    from datetime import datetime, timedelta

    where_msg, params_msg = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=date_str, end=date_str, client=client_source, table_alias='m'
    )
    join_ss_msg = _join_session_sources('m')

    sessions_raw = run_query(conn, f"""
        SELECT
            m.session_id,
            m.project_id,
            MIN(m.timestamp) AS start_ts,
            MAX(m.timestamp) AS end_ts,
            COUNT(*) AS msg_count,
            SUM(CASE WHEN m.type = 'user' THEN 1 ELSE 0 END) AS user_msgs,
            SUM(CASE WHEN m.type = 'assistant' THEN 1 ELSE 0 END) AS assistant_msgs
        FROM messages m
        {join_ss_msg}
        {where_msg}
        GROUP BY m.session_id, m.project_id
        ORDER BY start_ts
    """, params_msg)

    empty_worktime = {
        "active_wallclock_sec": 0.0,
        "assistant_wallclock_sec": 0.0,
        "active_gap_sec": active_gap_sec,
        "assistant_cap_sec": assistant_cap_sec,
    }

    if not sessions_raw:
        return {
            "date": date_str,
            "rows": [],
            "projects": [],
            "concurrency": [],
            "peak_concurrency": 0,
            "peak_at": None,
            "worktime": empty_worktime,
            "meta": {
                "total_sessions": 0,
                "total_projects": 0,
                "total_messages": 0,
                "subagent_invocations": 0,
            },
        }

    keys = [(s["session_id"], s["project_id"]) for s in sessions_raw]
    placeholders = ",".join(["(?,?)" for _ in keys])
    flat = [v for k in keys for v in k]
    enriched_rows = run_query(conn, f"""
        SELECT
            c.session_id, c.project_id,
            c.summary, c.display, c.parent_session_id, c.agent_id,
            p.name AS project_name,
            COALESCE(ss.source, 'unknown') AS source
        FROM interactions c
        LEFT JOIN projects p ON p.id = c.project_id
        LEFT JOIN session_sources ss ON ss.session_id = c.session_id AND ss.project_id = c.project_id
        WHERE (c.session_id, c.project_id) IN ({placeholders})
    """, flat)
    meta_map = {(r["session_id"], r["project_id"]): r for r in enriched_rows}

    msg_rows = run_query(conn, f"""
        SELECT m.session_id, m.project_id, m.type, m.timestamp
        FROM messages m
        {join_ss_msg}
        {where_msg}
        ORDER BY m.session_id, m.project_id, m.timestamp
    """, params_msg)

    def _parse(ts: str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    by_sess = {}
    for r in msg_rows:
        by_sess.setdefault((r["session_id"], r["project_id"]), []).append(r)

    active_intervals = []
    asst_intervals = []
    for recs in by_sess.values():
        if not recs:
            continue
        cs = _parse(recs[0]["timestamp"])
        ce = cs
        for r in recs[1:]:
            t = _parse(r["timestamp"])
            if (t - ce).total_seconds() <= active_gap_sec:
                ce = t
            else:
                active_intervals.append((cs, ce))
                cs, ce = t, t
        active_intervals.append((cs, ce))
        for i in range(len(recs) - 1):
            if recs[i]["type"] == "user":
                for j in range(i + 1, len(recs)):
                    if recs[j]["type"] == "assistant":
                        t_u = _parse(recs[i]["timestamp"])
                        t_a = _parse(recs[j]["timestamp"])
                        delta = (t_a - t_u).total_seconds()
                        if delta <= assistant_cap_sec:
                            asst_intervals.append((t_u, t_a))
                        else:
                            asst_intervals.append((t_u, t_u + timedelta(seconds=assistant_cap_sec)))
                        break

    def _merge(iv):
        if not iv:
            return []
        iv = sorted(iv)
        out = [list(iv[0])]
        for s, e in iv[1:]:
            if s <= out[-1][1]:
                out[-1][1] = max(out[-1][1], e)
            else:
                out.append([s, e])
        return out

    active_merged = _merge(active_intervals)
    asst_merged = _merge(asst_intervals)
    active_sec = sum((e - s).total_seconds() for s, e in active_merged)
    asst_sec = sum((e - s).total_seconds() for s, e in asst_merged)

    rows = []
    for s in sessions_raw:
        key = (s["session_id"], s["project_id"])
        meta = meta_map.get(key, {})
        start = _parse(s["start_ts"])
        end = _parse(s["end_ts"])
        parent_sid = meta.get("parent_session_id") or ""
        rows.append({
            "session_id": s["session_id"],
            "project_id": s["project_id"],
            "project_name": meta.get("project_name") or "unknown",
            "source": meta.get("source") or "unknown",
            "start": s["start_ts"],
            "end": s["end_ts"],
            "duration_min": (end - start).total_seconds() / 60.0,
            "msg_count": s["msg_count"],
            "user_msgs": s["user_msgs"],
            "assistant_msgs": s["assistant_msgs"],
            "summary": meta.get("summary") or "",
            "display": meta.get("display") or "",
            "parent_session_id": parent_sid or None,
            "agent_id": meta.get("agent_id") or None,
            "is_subagent": bool(parent_sid),
        })

    palette = ["#35C2FF", "#F9C927", "#00D35D", "#ff6ab5", "#b48cff",
               "#ff9a3c", "#888fad", "#5c6789"]
    by_proj = {}
    for r in rows:
        by_proj.setdefault(r["project_name"], []).append(r)
    proj_totals = {p: sum(r["duration_min"] for r in rs) for p, rs in by_proj.items()}
    sorted_projs = sorted(proj_totals.items(), key=lambda x: -x[1])
    projects = []
    for i, (name, total) in enumerate(sorted_projs):
        projects.append({
            "name": name,
            "color": palette[i % len(palette)],
            "session_count": len(by_proj[name]),
            "total_min": round(total, 2),
        })

    events = []
    for r in rows:
        events.append((_parse(r["start"]), +1))
        events.append((_parse(r["end"]), -1))
    events.sort(key=lambda x: (x[0], -x[1]))
    cur = 0
    peak = 0
    peak_at = None
    concurrency_curve = []
    for t, d in events:
        concurrency_curve.append({"t": t.isoformat(), "c": cur})
        cur += d
        concurrency_curve.append({"t": t.isoformat(), "c": cur})
        if cur > peak:
            peak = cur
            peak_at = t.isoformat()

    where_sa, params_sa = build_filters(
        project_id=project_id, user_id=user_id, host_id=host_id,
        start=date_str, end=date_str, client=client_source, table_alias='sa'
    )
    join_ss_sa = _join_session_sources('sa')
    subagent_count = run_query(conn, f"""
        SELECT COUNT(*) AS n FROM subagent_invocations sa
        {join_ss_sa}
        {where_sa}
    """, params_sa)[0]["n"]

    return {
        "date": date_str,
        "rows": rows,
        "projects": projects,
        "concurrency": concurrency_curve,
        "peak_concurrency": peak,
        "peak_at": peak_at,
        "worktime": {
            "active_wallclock_sec": round(active_sec, 2),
            "assistant_wallclock_sec": round(asst_sec, 2),
            "active_gap_sec": active_gap_sec,
            "assistant_cap_sec": assistant_cap_sec,
        },
        "meta": {
            "total_sessions": len(rows),
            "total_projects": len(projects),
            "total_messages": sum(r["msg_count"] for r in rows),
            "subagent_invocations": subagent_count,
            "subagent_sessions": sum(1 for r in rows if r["is_subagent"]),
        },
    }

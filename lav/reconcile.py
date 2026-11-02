"""
Reconcile by differences between a remote agent and this node (LAV-98).

The pull cursor cannot guarantee completeness. `last_pull:<agent>` advances to the
newest timestamp received from any session, and the export filters child rows on the
time of the EVENT, while a row reaches the agent's DB when the parser WRITES it. A row
written after the cursor has passed its timestamp never ships, and nothing logs it.
So the cursor stays for intra-day freshness, and this is the guarantee:

    agent manifest -> local manifest -> compare -> export the sessions that differ,
    whole -> ingest (guarded inserts, so idempotent) -> recompute totals -> recount

Python and SQL only, no inference. It runs on the collector and writes the local DB
only, so like `lav backfill` it needs no API key. `run_scheduled()` adds the
once-a-day rule and the notifications for the hourly LaunchAgent.
"""

import http.client
import json
import os
import shlex
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from lav.parsers.jsonl import (
    get_parse_state,
    ingest_remote_sessions,
    is_real_agent_id,
    set_parse_state,
)
from lav.queries import EXPORT_CHILD_TABLES, session_manifest, session_projects

DEFAULT_DAYS = 7
DEFAULT_SETTLE_MINUTES = 60
DEFAULT_BUSY_TIMEOUT = 600  # seconds: a running lav-parse can hold the write lock for ~9 minutes

# One export request. http.server refuses a request line over 64 KB, and a 20,000-row
# batch is what the collector can hold in memory and ingest in one short transaction.
# The row limit is an estimate from the manifest; a single larger session goes alone.
BATCH_MAX_SESSIONS = 50
BATCH_MAX_URL_BYTES = 60_000
BATCH_MAX_ROWS = 20_000
REQUEST_MIN_TIMEOUT = 600  # seconds, whatever the agent's timeout_seconds says

STILL_DIFFERENT_CAP = 20

STATUS_OK = "ok"
STATUS_DIFFERENCES = "differences"
STATUS_UNREACHABLE = "unreachable"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"  # --scheduled only: today's run is already done

# "Completed" = the comparison ran to the end. Only these stop the hourly retries.
COMPLETED_STATUSES = (STATUS_OK, STATUS_DIFFERENCES)

_EXIT_CODES = {STATUS_OK: 0, STATUS_SKIPPED: 0, STATUS_DIFFERENCES: 2,
               STATUS_UNREACHABLE: 3, STATUS_ERROR: 1}
_WORST_FIRST = (STATUS_ERROR, STATUS_UNREACHABLE, STATUS_DIFFERENCES, STATUS_OK, STATUS_SKIPPED)

# --scheduled: the "day" starts at 03:30 local time, the 30-day window on Sunday.
DAY_START_HOUR, DAY_START_MINUTE = 3, 30
WINDOW_DAILY_DAYS = 7
WINDOW_WEEKLY_DAYS = 30
OUTAGE_ALERT_AFTER = timedelta(hours=48)
NOTIFY_TIMEOUT = 60  # seconds


class AgentUnreachable(Exception):
    """No URL of the agent answered. Normal for a laptop: a status, not an error."""


class AgentError(Exception):
    """The agent answered, but not with what we need (e.g. 404: old code)."""


# ===========================================================================
# CLOCK AND WINDOW
# ===========================================================================

def _local_now() -> datetime:
    return datetime.now().astimezone()


def _iso_z(moment: datetime) -> str:
    """Same shape as the Claude Code timestamps these values are compared with, as strings."""
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _parse_since(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        raise ValueError(f"--since '{value}' is not an ISO date") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _iso_z(parsed)


def window(now=None, days=None, since=None, settle_minutes=DEFAULT_SETTLE_MINUTES) -> tuple:
    """(since, until) for a run. Raises ValueError on an unreadable `since`.

    Rows newer than `until` are ignored on both sides: otherwise every live session
    looks different and "zero differences" can never pass.
    """
    now = now or _local_now()
    until = _iso_z(now - timedelta(minutes=settle_minutes))
    if since is not None:
        return _parse_since(since), until
    return _iso_z(now - timedelta(days=DEFAULT_DAYS if days is None else days)), until


# ===========================================================================
# LOCAL DB
# ===========================================================================

def open_db(db_path, busy_timeout=DEFAULT_BUSY_TIMEOUT, read_only=False) -> sqlite3.Connection:
    """A plain connection, deliberately not init_db().

    init_db() seeds and migrates under its own 5-second busy timeout, so a running
    lav-parse would fail this command right at the door. The schema is the parser's and
    the server's job; if it is behind, the first batch fails with a clear OperationalError.
    Read-only means read-only: --dry-run opens with mode=ro and applies no DDL.
    """
    if read_only:
        conn = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=busy_timeout)
        conn.execute("PRAGMA query_only=ON")
        return conn
    return sqlite3.connect(str(db_path), timeout=busy_timeout)


class _NoCommit:
    """`conn` with commit() disabled, for ingest_remote_sessions().

    It commits on its way out, and the totals have to land in the same transaction as
    the rows: a failure between the two would leave stale totals that no later run
    fixes, because by then the session no longer looks different.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def commit(self):
        pass


def recompute_totals(conn, keys) -> None:
    """Rebuild total_tokens and message_count from the rows of these sessions.

    Same canonical formula as ingest_remote_sessions() and update_interaction(): the
    token_usage sum, falling back to the messages' tokens. It is here because the
    ingest skips a session with no messages at all, even when it has token rows.
    A session with no rows of either kind has nothing to derive from: left alone.
    Does not commit.
    """
    projects = session_projects(conn, [key[0] for key in keys])
    session_keys = [(session_id, project["project_id"])
                    for session_id, name in keys
                    for project in projects.get(session_id, [])
                    if project["project_name"] == name]
    if not session_keys:
        return
    placeholders = ",".join(["(?,?)" for _ in session_keys])
    flat_params = [value for key in session_keys for value in key]
    messages = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(f"""
        SELECT session_id, project_id, COUNT(*), SUM(tokens_in + tokens_out)
        FROM messages WHERE (session_id, project_id) IN ({placeholders})
        GROUP BY session_id, project_id
    """, flat_params)}
    tokens = {(r[0], r[1]): r[2] for r in conn.execute(f"""
        SELECT session_id, project_id,
               SUM(input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens)
        FROM token_usage WHERE (session_id, project_id) IN ({placeholders})
        GROUP BY session_id, project_id
    """, flat_params)}
    for key in session_keys:
        if key not in messages and key not in tokens:
            continue
        message_count, message_tokens = messages.get(key, (0, 0))
        total = tokens.get(key)
        if total is None:
            total = message_tokens or 0
        conn.execute("""
            UPDATE interactions SET total_tokens = ?, message_count = ?
            WHERE session_id = ? AND project_id = ?
              AND (total_tokens IS NOT ? OR message_count IS NOT ?)
        """, (total, message_count, key[0], key[1], total, message_count))


# ===========================================================================
# TALKING TO THE AGENT
# ===========================================================================

def _agent_urls(agent) -> list:
    urls = [agent["url"]]
    if agent.get("fallback_url"):
        urls.append(agent["fallback_url"])
    return urls


def _get_json(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def _fetch_manifest(agent, since, until, timeout) -> tuple:
    """(url that answered, manifest), trying the URLs in order like pull_from_agents."""
    query = urlencode({"since": since, "until": until})
    unreachable = []
    failure = None
    for url in _agent_urls(agent):
        try:
            return url, _get_json(f"{url}/api/manifest?{query}", timeout)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                failure = (f"{url} has no /api/manifest (HTTP 404): the agent runs code older "
                           f"than LAV-98, update it and restart its server")
            else:
                failure = f"{url}/api/manifest answered HTTP {e.code} {e.reason}"
        except (OSError, http.client.HTTPException) as e:
            unreachable.append(f"{url}: {e}")
        except ValueError as e:
            failure = f"{url}/api/manifest sent unreadable JSON: {e}"
    if failure:
        raise AgentError(failure)
    raise AgentUnreachable("; ".join(unreachable))


def _keys_param(keys) -> str:
    return quote(json.dumps([list(key) for key in keys], separators=(",", ":")), safe="")


def plan_batches(items, url_prefix_len=0) -> list:
    """Split [(key, estimated_rows)] into export requests, keeping the order."""
    batches, current, current_rows = [], [], 0
    for key, rows in items:
        if current and (len(current) >= BATCH_MAX_SESSIONS
                        or current_rows + rows > BATCH_MAX_ROWS
                        or url_prefix_len + len(_keys_param(current + [key])) > BATCH_MAX_URL_BYTES):
            batches.append(current)
            current, current_rows = [], 0
        current.append(key)
        current_rows += rows
    if current:
        batches.append(current)
    return batches


def _copy_batch(conn, batch, urls, timeout):
    """Fetch and ingest one batch, with its totals, in one transaction.

    Returns None, or (kind, message) on failure, with kind "unreachable" or "error".
    """
    param = _keys_param(batch)
    package = None
    failure = None
    for url in urls:
        try:
            package = _get_json(f"{url}/api/export/sessions?keys={param}", timeout)
            break
        except urllib.error.HTTPError as e:
            failure = (STATUS_ERROR, f"{url}/api/export/sessions answered HTTP {e.code} {e.reason}")
        except (OSError, http.client.HTTPException) as e:
            failure = (STATUS_UNREACHABLE, f"{url}: {e}")
        except ValueError as e:
            failure = (STATUS_ERROR, f"{url}/api/export/sessions sent unreadable JSON: {e}")
    if package is None:
        return failure
    try:
        ingest_remote_sessions(_NoCommit(conn), package.get("sessions", []),
                               package.get("host", {}), package.get("user", {}))
        recompute_totals(conn, batch)
        conn.commit()
    except Exception as e:
        conn.rollback()
        return (STATUS_ERROR, f"{type(e).__name__}: {e}")
    return None


# ===========================================================================
# COMPARE
# ===========================================================================

def _is_phantom(session_id) -> bool:
    """LAV-66: a meta "child" the ingest refuses, so it can never line up."""
    return "::agent-" in session_id and not is_real_agent_id(session_id.split("::agent-", 1)[1])


def _compare_session(agent_session, local_session, until) -> dict:
    """Compare one session's counts.

    `differs`: any table where count or max_ts differs (the session gets exported).
    `missing`: per table, max(0, agent - local). `collector_only`: sum of max(0, local - agent).
    `lost`: something provably missing here, which is what makes a run fail. Besides a
    positive `missing`, that is the agent's newest row being newer than ours, and equal
    counts with different max_ts: one row here is not on the agent, so with equal counts
    one row of the agent's is not here. More rows here with an equal or older agent
    max_ts prove nothing: those are the rows reported as collector-only, not a failure.
    """
    agent_tables = agent_session["tables"]
    if local_session is None:
        # Too new to matter on both sides, unless it holds rows old enough to compare
        # or it is a session with no rows at all, whose interaction row is what is missing.
        last_activity = agent_session.get("last_activity")
        absent = bool(agent_tables) or (last_activity is not None and last_activity <= until)
        return {
            "differs": absent, "lost": absent, "absent": absent, "collector_only": 0,
            "missing": {table: v["count"] for table, v in agent_tables.items()},
            "tables": {table: {"agent": v["count"], "local": 0, "agent_max_ts": v["max_ts"],
                               "local_max_ts": None} for table, v in agent_tables.items()},
        }

    empty = {"count": 0, "max_ts": None}
    local_tables = local_session["tables"]
    result = {"differs": False, "lost": False, "absent": False,
              "collector_only": 0, "missing": {}, "tables": {}}
    for table in EXPORT_CHILD_TABLES:
        ours = local_tables.get(table, empty)
        theirs = agent_tables.get(table, empty)
        if ours["count"] == theirs["count"] and ours["max_ts"] == theirs["max_ts"]:
            continue
        result["differs"] = True
        result["tables"][table] = {"agent": theirs["count"], "local": ours["count"],
                                   "agent_max_ts": theirs["max_ts"], "local_max_ts": ours["max_ts"]}
        gap = theirs["count"] - ours["count"]
        if gap > 0:
            result["missing"][table] = gap
        else:
            result["collector_only"] += -gap
        if gap >= 0 or (theirs["max_ts"] or "") > (ours["max_ts"] or ""):
            result["lost"] = True
    return result


def _local_by_key(conn, keys, until) -> dict:
    manifest = session_manifest(conn, until=until, keys=keys)
    return {(s["session_id"], s["project_name"]): s for s in manifest}


# ===========================================================================
# THE RUN
# ===========================================================================

def _new_summary(name, since, until, dry_run) -> dict:
    return {
        "agent": name,
        "status": STATUS_ERROR,
        "since": since,
        "until": until,
        "dry_run": dry_run,
        "sessions_compared": 0,
        "rows_compared": 0,
        "rows_missing": {},
        "rows_missing_total": 0,
        "rows_recovered": {},
        "rows_recovered_total": 0,
        "rows_still_missing": 0,
        "rows_collector_only": 0,
        "sessions_different": 0,
        "sessions_with_missing_rows": 0,
        "sessions_still_different": [],
        "sessions_still_different_total": 0,
        "other_project": [],
        "phantom_skipped": 0,
        "failed_batches": [],
        "url": None,
        "error": None,
        "duration_s": 0,
    }


def reconcile_agent(conn, agent, since, until, dry_run=False, log=None) -> dict:
    """One agent, start to finish. Never raises: the outcome is in the summary."""
    started = time.monotonic()
    summary = _new_summary(agent["name"], since, until, dry_run)
    try:
        _reconcile(conn, agent, summary, log or (lambda message: None))
    except AgentUnreachable as e:
        summary.update(status=STATUS_UNREACHABLE, error=str(e))
    except AgentError as e:
        summary.update(status=STATUS_ERROR, error=str(e))
    except Exception as e:
        conn.rollback()
        summary.update(status=STATUS_ERROR, error=f"{type(e).__name__}: {e}")
    summary["duration_s"] = round(time.monotonic() - started, 1)
    return summary


def _reconcile(conn, agent, summary, log) -> None:
    name, since, until, dry_run = agent["name"], summary["since"], summary["until"], summary["dry_run"]
    timeout = max(agent.get("timeout_seconds", 0), REQUEST_MIN_TIMEOUT)

    # 1-2. Agent manifest, then ours for the same keys and the same `until`.
    url, manifest = _fetch_manifest(agent, since, until, timeout)
    summary["url"] = url
    sessions = {}
    for session in manifest.get("sessions", []):
        if _is_phantom(session["session_id"]):
            summary["phantom_skipped"] += 1
        else:
            sessions[(session["session_id"], session["project_name"])] = session
    log(f"{name}: {len(sessions)} sessions in the agent's manifest from {url}")

    before = _local_by_key(conn, list(sessions), until)

    # A session living here under another project is NOT absent: ingesting it would
    # create a second copy and double its costs. Reported, never touched.
    absent = [key for key in sessions if key not in before]
    elsewhere = session_projects(conn, [key[0] for key in absent])
    for key in absent:
        others = [p["project_name"] for p in elsewhere.get(key[0], []) if p["project_name"] != key[1]]
        if others:
            session = sessions.pop(key)
            summary["other_project"].append({
                "session_id": key[0],
                "agent_project": key[1],
                "local_projects": others,
                "agent_rows": sum(v["count"] for v in session["tables"].values()),
            })

    # 3. Compare.
    different = []
    for key, session in sessions.items():
        compared = _compare_session(session, before.get(key), until)
        summary["rows_compared"] += sum(v["count"] for v in session["tables"].values())
        for table, gap in compared["missing"].items():
            summary["rows_missing"][table] = summary["rows_missing"].get(table, 0) + gap
        if compared["missing"]:
            summary["sessions_with_missing_rows"] += 1
        if compared["differs"]:
            different.append(key)
    summary["sessions_compared"] = len(sessions)
    summary["sessions_different"] = len(different)
    summary["rows_missing_total"] = sum(summary["rows_missing"].values())
    log(f"{name}: {len(different)} sessions differ, {summary['rows_missing_total']} rows missing")

    # 4-5. Copy what differs, batch by batch. A failed batch is rolled back and recorded,
    # and the run goes on: the other sessions are still worth having.
    after = before
    if different and not dry_run:
        items = [(key, sum(v["count"] for v in sessions[key]["tables"].values())) for key in different]
        urls = [url] + [other for other in _agent_urls(agent) if other != url]
        prefix = max(len(u) for u in urls) + len("/api/export/sessions?keys=")
        batches = plan_batches(items, prefix)
        for number, batch in enumerate(batches, 1):
            batch_started = time.monotonic()
            failure = _copy_batch(conn, batch, urls, timeout)
            if failure:
                summary["failed_batches"].append(
                    {"sessions": len(batch), "kind": failure[0], "error": failure[1]})
            log(f"{name}: batch {number}/{len(batches)}, {len(batch)} sessions, "
                f"{'FAILED: ' + failure[1] if failure else 'ok'} "
                f"({time.monotonic() - batch_started:.1f}s)")

        # 6. Count again.
        after = _local_by_key(conn, different, until)

    _finish(summary, sessions, different, before, after, until)


def _finish(summary, sessions, different, before, after, until) -> None:
    """Final numbers and status, from the counts as they are now."""
    lost = False
    still_different = []
    for key in different:
        compared = _compare_session(sessions[key], after.get(key), until)
        summary["rows_still_missing"] += sum(compared["missing"].values())
        summary["rows_collector_only"] += compared["collector_only"]
        lost = lost or compared["lost"]
        if compared["differs"]:
            still_different.append({"session_id": key[0], "project_name": key[1],
                                    "absent": compared["absent"], "tables": compared["tables"]})

        tables_before = before[key]["tables"] if key in before else {}
        tables_after = after[key]["tables"] if key in after else {}
        for table in EXPORT_CHILD_TABLES:
            gained = (tables_after.get(table, {}).get("count", 0)
                      - tables_before.get(table, {}).get("count", 0))
            if gained > 0:
                summary["rows_recovered"][table] = summary["rows_recovered"].get(table, 0) + gained

    summary["rows_recovered_total"] = sum(summary["rows_recovered"].values())
    summary["sessions_still_different"] = still_different[:STILL_DIFFERENT_CAP]
    summary["sessions_still_different_total"] = len(still_different)

    failures = summary["failed_batches"]
    if not lost:
        summary["status"] = STATUS_OK
    elif not failures:
        summary["status"] = STATUS_DIFFERENCES
    else:
        hard = [f for f in failures if f["kind"] == STATUS_ERROR]
        summary["status"] = STATUS_ERROR if hard else STATUS_UNREACHABLE
        summary["error"] = (hard or failures)[0]["error"]


def exit_code(summaries) -> int:
    """0 ok, 2 differences remain, 3 agent unreachable, 1 error. The worst agent wins."""
    statuses = {s["status"] for s in summaries}
    for status in _WORST_FIRST:
        if status in statuses:
            return _EXIT_CODES[status]
    return 0


def format_brief(summary) -> str:
    """One line per real run, for the log."""
    parts = [summary["agent"], summary["status"]]
    if summary.get("window_days"):
        parts.append(f"{summary['window_days']}d")
    parts += [
        f"sessions={summary['sessions_compared']}",
        f"rows={summary['rows_compared']}",
        f"missing={summary['rows_missing_total']}",
        f"recovered={summary['rows_recovered_total']}",
        f"still_missing={summary['rows_still_missing']}",
        f"collector_only={summary['rows_collector_only']}",
        f"different={summary['sessions_different']}",
        f"still_different={summary['sessions_still_different_total']}",
        f"other_project={len(summary['other_project'])}",
        f"phantom={summary['phantom_skipped']}",
        f"{summary['duration_s']}s",
    ]
    if summary.get("notify"):
        parts.append("notify=" + ",".join(f"{n['kind']}:{n['result']}" for n in summary["notify"]))
    if summary["error"]:
        parts.append("error=" + " ".join(str(summary["error"]).split()))
    if summary.get("state_error"):
        parts.append("state_error=" + " ".join(str(summary["state_error"]).split()))
    return " ".join(parts)


# ===========================================================================
# SCHEDULED MODE
# ===========================================================================

def day_start(now: datetime) -> datetime:
    """The most recent 03:30 local. `now` carries the local timezone."""
    start = now.replace(hour=DAY_START_HOUR, minute=DAY_START_MINUTE, second=0, microsecond=0)
    return start if now >= start else start - timedelta(days=1)


def week_start(now: datetime) -> datetime:
    """The most recent Sunday 03:30 local."""
    start = day_start(now)
    return start - timedelta(days=(start.weekday() + 1) % 7)


def scheduled_window(ok_7, ok_30, now):
    """Days to compare now, or None if a run already completed in this "day".

    It tries every hour from 03:30 until one attempt completes (the laptop agent sleeps
    about one night in four), then stops until the next day. 30 days if no 30-day run
    completed since the most recent Sunday 03:30: Sunday, or the first completed run after
    it when the laptop was off.
    """
    start = day_start(now)
    if any(stamp is not None and stamp >= start for stamp in (ok_7, ok_30)):
        return None
    if ok_30 is None or ok_30 < week_start(now):
        return WINDOW_WEEKLY_DAYS
    return WINDOW_DAILY_DAYS


def _get_state(conn, key):
    return get_parse_state(conn, key, project_id=-1, source="remote", host_id=-1)


def _set_state(conn, key, value) -> None:
    set_parse_state(conn, key, value, project_id=-1, source="remote", host_id=-1)


def _clear_state(conn, key) -> None:
    conn.execute("DELETE FROM parse_state "
                 "WHERE key = ? AND project_id = -1 AND source = 'remote' AND host_id = -1", (key,))


def _parse_stamp(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _load_state(conn, name) -> dict:
    ok_7 = _parse_stamp(_get_state(conn, f"reconcile_ok:{name}:{WINDOW_DAILY_DAYS}"))
    ok_30 = _parse_stamp(_get_state(conn, f"reconcile_ok:{name}:{WINDOW_WEEKLY_DAYS}"))
    return {
        "ok_7": ok_7,
        "ok_30": ok_30,
        "last_completed": max((s for s in (ok_7, ok_30) if s is not None), default=None),
        "first_attempt": _parse_stamp(_get_state(conn, f"reconcile_first_attempt:{name}")),
        "error_notified": _get_state(conn, f"reconcile_error_notified:{name}"),
        "outage_notified": _get_state(conn, f"reconcile_outage_notified:{name}"),
    }


def _plain(text) -> str:
    """The notify script parses HTML: no <, > or &. And one short line."""
    cleaned = " ".join(str(text).replace("<", " ").replace(">", " ").replace("&", " e ").split())
    return cleaned[:300]


def _count_it(count, one, many) -> str:
    return f"{count} {one if count == 1 else many}"


def scheduled_notifications(summary, state, now) -> list:
    """[(kind, message)] owed for this run. No side effects.

    missing: a completed run that found missing rows, recovered or not.
    error:   status error, at most once per calendar day.
    outage:  no completed run for 48 hours, once per outage.
    A completed run with nothing missing never notifies.
    """
    name, status = summary["agent"], summary["status"]
    if status in COMPLETED_STATUSES:
        found = summary["rows_missing_total"]
        recovered = summary["rows_recovered_total"]
        if not (found or recovered or status == STATUS_DIFFERENCES):
            return []
        message = (f"LAV reconcile {name} ({summary.get('window_days')} giorni): trovate "
                   f"{_count_it(found, 'riga mancante', 'righe mancanti')} in "
                   f"{_count_it(summary['sessions_with_missing_rows'], 'sessione', 'sessioni')}, "
                   f"recuperate {recovered}, ancora mancanti {summary['rows_still_missing']}.")
        if status == STATUS_DIFFERENCES and not summary["rows_still_missing"]:
            message += f" Sessioni ancora diverse: {summary['sessions_still_different_total']}."
        return [("missing", _plain(message))]

    owed = []
    if status == STATUS_ERROR and state["error_notified"] != now.date().isoformat():
        owed.append(("error", _plain(f"LAV reconcile {name}: errore, confronto non completato. "
                                     f"{summary['error']}")))
    since = state["last_completed"] or state["first_attempt"]
    if since is not None and now - since >= OUTAGE_ALERT_AFTER and not state["outage_notified"]:
        hours = int((now - since).total_seconds() // 3600)
        if state["last_completed"]:
            detail = f"Ultimo completato: {state['last_completed'].strftime('%Y-%m-%d %H:%M')}."
        else:
            detail = "Nessun confronto completato finora."
        owed.append(("outage", _plain(f"LAV reconcile {name}: nessun confronto completato da "
                                      f"{hours} ore (agente spento o non raggiungibile). {detail}")))
    return owed


def send_notification(message) -> str:
    """Run LAV_NOTIFY_CMD with the message as one argument: argument list, never a shell.

    Returns the outcome for the log. Never raises: a failing notify is not a failing run.
    """
    command = os.environ.get("LAV_NOTIFY_CMD", "").strip()
    if not command:
        return "skipped(LAV_NOTIFY_CMD not set)"
    try:
        subprocess.run(shlex.split(command) + [message],
                       check=True, capture_output=True, timeout=NOTIFY_TIMEOUT)
    except subprocess.CalledProcessError as e:
        return f"failed(exit {e.returncode})"
    except subprocess.TimeoutExpired:
        return "failed(timeout)"
    except (OSError, ValueError) as e:
        return f"failed({e})"
    return "sent"


def _write_state(conn, summary, writes) -> None:
    """Apply state changes. Never fatal: at worst the run is repeated within the hour."""
    try:
        for key, value in writes:
            if value is None:
                _clear_state(conn, key)
            else:
                _set_state(conn, key, value)
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        summary["state_error"] = str(e)


def run_scheduled(conn, agents, settle_minutes=DEFAULT_SETTLE_MINUTES, now=None, notifier=None) -> list:
    """The hourly tick: for each agent, at most one completed run per "day". `now` is injectable."""
    now = now or _local_now()
    notifier = notifier or send_notification
    summaries = []
    for agent in agents:
        name = agent["name"]
        state = _load_state(conn, name)
        days = scheduled_window(state["ok_7"], state["ok_30"], now)
        if days is None:
            summaries.append({"agent": name, "status": STATUS_SKIPPED})
            continue

        since, until = window(now=now, days=days, settle_minutes=settle_minutes)
        summary = reconcile_agent(conn, agent, since, until)
        summary["window_days"] = days

        if summary["status"] in COMPLETED_STATUSES:
            _write_state(conn, summary, [(f"reconcile_ok:{name}:{days}", now.isoformat()),
                                         (f"reconcile_outage_notified:{name}", None),
                                         (f"reconcile_first_attempt:{name}", None)])
        elif state["last_completed"] is None and state["first_attempt"] is None:
            # The 48 hours need a start even when no run ever completed.
            state["first_attempt"] = now
            _write_state(conn, summary, [(f"reconcile_first_attempt:{name}", now.isoformat())])

        summary["notify"] = []
        for kind, message in scheduled_notifications(summary, state, now):
            result = notifier(message)
            summary["notify"].append({"kind": kind, "message": message, "result": result})
            if result == "sent" and kind == "error":
                _write_state(conn, summary, [(f"reconcile_error_notified:{name}", now.date().isoformat())])
            elif result == "sent" and kind == "outage":
                _write_state(conn, summary, [(f"reconcile_outage_notified:{name}", now.isoformat())])
        summaries.append(summary)
    return summaries

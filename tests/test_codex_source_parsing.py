"""Tests for LAV-74: Codex watermark (UTC) + source attribution from originator.

Run manually:
    python tests/test_codex_source_parsing.py

Mirrors the standalone eval-script style of tests/test_title_parsing.py — no
pytest. Builds temp Codex rollout JSONL + a temp DB, drives parse_codex_sessions
directly (via codex_sessions_dirs=[tmp]) and asserts on watermark, source
attribution, incremental de-dup, and full-reparse behaviour.

Covered:
  1. A UTC event newer than a *legacy naive local* watermark is imported
     (the "recent sessions lost" bug: lexical string compare skipped it).
  2. Several rollouts of the same project in one pass -> final watermark equals
     the max imported event timestamp.
  3. Incremental rerun imports nothing new and does not duplicate rows in
     tables without a UNIQUE constraint (bash_commands).
  4. originator -> source mapping for Work/Desktop/VS Code/CLI + unknown
     fallback to codex_local (raw originator kept in meta_json).
  5. upsert override: a stale codex_cli row is upgraded to a recognized
     surface; a non-Codex source is left untouched.
  6. --full ignores the watermark and re-imports everything (no duplicates).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

# Make `import lav` work when running directly from the repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Force a known positive-offset local tz so naive-watermark math is deterministic
# regardless of the host. Europe/Rome = UTC+2 in July.
os.environ["TZ"] = "Europe/Rome"
try:
    time.tzset()
except AttributeError:  # pragma: no cover (non-Unix)
    pass

from lav.config import (  # noqa: E402
    SOURCE_CODEX_CLI,
    SOURCE_CHATGPT_WORK_DESKTOP,
    SOURCE_CODEX_DESKTOP,
    SOURCE_CODEX_VSCODE,
    SOURCE_CODEX_LOCAL,
    SOURCE_CLAUDE_AI,
)
from lav.parsers.jsonl import (  # noqa: E402
    init_db,
    parse_codex_sessions,
    map_codex_source,
    upsert_session_source,
    format_codex_session_id,
    get_or_create_project,
    get_parse_state,
    _parse_codex_event_ts,
    _parse_codex_watermark_ts,
)


def _rollout(path: Path, session_id: str, cwd: str, originator: str, events: list):
    """Write a minimal Codex rollout: session_meta + given events (dicts)."""
    lines = [json.dumps({
        "timestamp": "2020-01-01T00:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "cwd": cwd,
            "originator": originator,
            "source": "vscode",
            "git": {"branch": "main"},
        },
    })]
    for ev in events:
        lines.append(json.dumps(ev))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _msg_event(ts: str, role: str, text: str) -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": "text", "text": text}]},
    }


def _shell_event(ts: str, command: str) -> dict:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "function_call", "name": "shell_command",
                    "arguments": json.dumps({"command": command, "workdir": "/tmp"})},
    }


def _mcp_event(ts: str, server: str) -> dict:
    # mcp_tool_calls has NO unique constraint and the Codex parser does a plain
    # INSERT OR IGNORE (no-op without a constraint) — so re-import duplicates it
    # unless the full-reparse wipe clears the session first.
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "function_call", "name": "read_mcp_resource",
                    "arguments": json.dumps({"server": server})},
    }


def _count(conn, table, session_id) -> int:
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
    ).fetchone()[0]


def _source_of(conn, session_id):
    row = conn.execute(
        "SELECT source, meta_json FROM session_sources WHERE session_id = ?", (session_id,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def run() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        marker = "OK " if ok else "FAIL"
        print(f"  [{marker}] {name}{('  ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    # --- helper-level sanity: tz parsing --------------------------------------
    ev = _parse_codex_event_ts("2026-07-12T09:30:00.000Z")
    wm = _parse_codex_watermark_ts("2026-07-12T11:00:00")  # naive local (Rome +2) = 09:00Z
    check("tz: UTC event 09:30Z > legacy local 11:00 (=09:00Z)",
          ev is not None and wm is not None and ev > wm,
          f"event={ev.isoformat()} watermark={wm.isoformat()}")

    # --- 4. originator -> source mapping (unit) -------------------------------
    cases = {
        "codex_work_desktop": (SOURCE_CHATGPT_WORK_DESKTOP, True),
        "Codex Desktop": (SOURCE_CODEX_DESKTOP, True),
        "codex_vscode": (SOURCE_CODEX_VSCODE, True),
        "codex_cli_rs": (SOURCE_CODEX_CLI, True),
        "codex-tui": (SOURCE_CODEX_CLI, True),
        "codex_exec": (SOURCE_CODEX_CLI, True),
        "banana_client": (SOURCE_CODEX_LOCAL, False),
        "": (SOURCE_CODEX_CLI, False),
    }
    for orig, (want_src, want_rec) in cases.items():
        got = map_codex_source(orig)
        check(f"map originator {orig!r}", got == (want_src, want_rec), f"got={got}")

    # ==========================================================================
    # DB-backed scenarios
    # ==========================================================================
    tmpdir = Path(tempfile.mkdtemp(prefix="lav_test_codex_"))
    sess_dir = tmpdir / "sessions"
    sess_dir.mkdir()
    proj_dir = tmpdir / "myproj"
    proj_dir.mkdir()
    cwd = str(proj_dir)
    db_path = tmpdir / "test.db"
    conn = init_db(db_path)

    # --- 1. legacy naive watermark must not hide a newer UTC event ------------
    sid1 = format_codex_session_id("11111111-1111-1111-1111-111111111111")
    _rollout(sess_dir / "r1.jsonl", "11111111-1111-1111-1111-111111111111", cwd, "codex_vscode",
             [_msg_event("2026-07-12T09:30:00.000Z", "user", "recent question")])
    # Seed the legacy watermark AFTER the project row exists.
    pid = get_or_create_project(conn, proj_dir.name, cwd)
    from lav.parsers.jsonl import detect_host, get_or_create_host, set_parse_state
    hn, ot, hd = detect_host()
    hid = get_or_create_host(conn, hn, ot, hd)
    set_parse_state(conn, "last_parsed", "2026-07-12T11:00:00", pid, SOURCE_CODEX_CLI, hid)  # naive local
    conn.commit()

    parse_codex_sessions(conn, full_reparse=False, codex_sessions_dirs=[sess_dir])
    check("1. newer UTC event imported past legacy local watermark",
          _count(conn, "messages", sid1) == 1, f"msgs={_count(conn, 'messages', sid1)}")
    check("1. source attributed codex_vscode", _source_of(conn, sid1)[0] == SOURCE_CODEX_VSCODE,
          f"src={_source_of(conn, sid1)[0]}")

    # --- 2. multiple rollouts same project -> watermark = max event -----------
    for name, sid, ts in [
        ("r2a.jsonl", "22222222-0000-0000-0000-000000000001", "2026-07-12T12:00:00.000Z"),
        ("r2b.jsonl", "22222222-0000-0000-0000-000000000002", "2026-07-12T14:30:00.000Z"),
        ("r2c.jsonl", "22222222-0000-0000-0000-000000000003", "2026-07-12T13:15:00.000Z"),
    ]:
        _rollout(sess_dir / name, sid, cwd, "codex_cli_rs", [_msg_event(ts, "user", "q")])
    parse_codex_sessions(conn, full_reparse=False, codex_sessions_dirs=[sess_dir])
    wm_raw = get_parse_state(conn, "last_parsed", pid, SOURCE_CODEX_CLI, hid)
    wm_dt = _parse_codex_watermark_ts(wm_raw)
    max_dt = _parse_codex_event_ts("2026-07-12T14:30:00.000Z")
    check("2. watermark == max imported event", wm_dt == max_dt, f"watermark={wm_raw}")

    # --- 3. incremental rerun: no new rows, no bash dup -----------------------
    sid3 = format_codex_session_id("33333333-3333-3333-3333-333333333333")
    _rollout(sess_dir / "r3.jsonl", "33333333-3333-3333-3333-333333333333", cwd, "codex_cli_rs",
             [_shell_event("2026-07-12T15:00:00.000Z", "ls -la"),
              _msg_event("2026-07-12T15:00:01.000Z", "assistant", "done")])
    parse_codex_sessions(conn, full_reparse=False, codex_sessions_dirs=[sess_dir])
    bash_after_1 = _count(conn, "bash_commands", sid3)
    msg_after_1 = _count(conn, "messages", sid3)
    parse_codex_sessions(conn, full_reparse=False, codex_sessions_dirs=[sess_dir])  # rerun
    check("3. incremental rerun: no bash duplication",
          _count(conn, "bash_commands", sid3) == bash_after_1 == 1,
          f"bash={_count(conn, 'bash_commands', sid3)}")
    check("3. incremental rerun: no message duplication",
          _count(conn, "messages", sid3) == msg_after_1 == 1,
          f"msgs={_count(conn, 'messages', sid3)}")

    # --- 4b. end-to-end attribution: Work Desktop + unknown fallback ----------
    sid_work = format_codex_session_id("44444444-4444-4444-4444-44444444aaaa")
    _rollout(sess_dir / "r4work.jsonl", "44444444-4444-4444-4444-44444444aaaa", cwd, "codex_work_desktop",
             [_msg_event("2026-07-12T16:00:00.000Z", "user", "work q")])
    sid_unk = format_codex_session_id("44444444-4444-4444-4444-44444444bbbb")
    _rollout(sess_dir / "r4unk.jsonl", "44444444-4444-4444-4444-44444444bbbb", cwd, "weird_surface_x",
             [_msg_event("2026-07-12T16:05:00.000Z", "user", "unknown q")])
    parse_codex_sessions(conn, full_reparse=False, codex_sessions_dirs=[sess_dir])
    check("4b. codex_work_desktop -> chatgpt_work_desktop",
          _source_of(conn, sid_work)[0] == SOURCE_CHATGPT_WORK_DESKTOP,
          f"src={_source_of(conn, sid_work)[0]}")
    unk_src, unk_meta = _source_of(conn, sid_unk)
    meta_ok = unk_meta is not None and json.loads(unk_meta).get("originator") == "weird_surface_x"
    check("4b. unknown originator -> codex_local, raw kept in meta",
          unk_src == SOURCE_CODEX_LOCAL and meta_ok, f"src={unk_src} meta={unk_meta}")

    # --- 5. upsert override of a stale codex_cli row --------------------------
    sid_up = format_codex_session_id("55555555-5555-5555-5555-555555555555")
    upsert_session_source(conn, sid_up, pid, SOURCE_CODEX_CLI)  # simulate legacy parse
    upsert_session_source(conn, sid_up, pid, SOURCE_CHATGPT_WORK_DESKTOP,
                          override_sources={SOURCE_CODEX_CLI})
    check("5. stale codex_cli upgraded to recognized surface",
          _source_of(conn, sid_up)[0] == SOURCE_CHATGPT_WORK_DESKTOP,
          f"src={_source_of(conn, sid_up)[0]}")
    # A non-Codex source must never be overridden by the Codex parser.
    sid_ai = format_codex_session_id("55555555-5555-5555-5555-5555aaaaaaaa")
    upsert_session_source(conn, sid_ai, pid, SOURCE_CLAUDE_AI)
    upsert_session_source(conn, sid_ai, pid, SOURCE_CHATGPT_WORK_DESKTOP,
                          override_sources={SOURCE_CODEX_CLI})
    check("5. non-Codex source left untouched",
          _source_of(conn, sid_ai)[0] == SOURCE_CLAUDE_AI, f"src={_source_of(conn, sid_ai)[0]}")

    # --- 6. --full ignores watermark and re-imports (no dup) ------------------
    # Push the watermark far into the future; incremental would import nothing.
    set_parse_state(conn, "last_parsed", "2030-01-01T00:00:00+00:00", pid, SOURCE_CODEX_CLI, hid)
    conn.commit()
    sid6 = format_codex_session_id("66666666-6666-6666-6666-666666666666")
    _rollout(sess_dir / "r6.jsonl", "66666666-6666-6666-6666-666666666666", cwd, "codex_cli_rs",
             [_mcp_event("2026-07-12T17:00:00.000Z", "atlassian"),
              _msg_event("2026-07-12T17:00:01.000Z", "user", "full q")])
    parse_codex_sessions(conn, full_reparse=False, codex_sessions_dirs=[sess_dir])
    check("6. incremental skips (future watermark)", _count(conn, "messages", sid6) == 0,
          f"msgs={_count(conn, 'messages', sid6)}")
    parse_codex_sessions(conn, full_reparse=True, codex_sessions_dirs=[sess_dir])
    check("6. --full re-imports despite watermark", _count(conn, "messages", sid6) == 1,
          f"msgs={_count(conn, 'messages', sid6)}")
    mcp6 = _count(conn, "mcp_tool_calls", sid6)
    parse_codex_sessions(conn, full_reparse=True, codex_sessions_dirs=[sess_dir])  # full again
    check("6. --full twice: no mcp_tool_calls duplication (wipe works)",
          _count(conn, "mcp_tool_calls", sid6) == mcp6 == 1,
          f"mcp={_count(conn, 'mcp_tool_calls', sid6)}")

    conn.close()

    if failures:
        print(f"\nFAIL: {len(failures)} check(s) failed: {failures}")
        return 1
    print("\nPASS: all Codex watermark + source-attribution checks")
    return 0


if __name__ == "__main__":
    sys.exit(run())

"""Test for ai-title / custom-title / legacy summary handling in jsonl parser.

Run manually:
    python tests/test_title_parsing.py

Mirrors the eval-script style of tests/evals/eval_classify.py — no pytest.
Creates a temp project dir + temp DB, writes 4 fixture sessions covering each
title source, runs parse_project, and asserts on interactions.summary.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make `import lav` work when running directly from the repo root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lav.parsers.jsonl import init_db, parse_project  # noqa: E402


def _msg(session_id: str, role: str, text: str, ts: str, uuid: str) -> dict:
    return {
        "type": role,
        "sessionId": session_id,
        "timestamp": ts,
        "uuid": uuid,
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
        "version": "2.1.120",
    }


def _write_session(jsonl_path: Path, session_id: str, title_records: list, prompt: str):
    """Write a fixture JSONL with title records + a single user prompt."""
    lines = []
    for rec in title_records:
        lines.append(json.dumps(rec))
    lines.append(json.dumps(_msg(session_id, "user", prompt, "2026-04-27T10:00:00.000Z", f"{session_id}-u1")))
    lines.append(json.dumps({
        "type": "assistant",
        "sessionId": session_id,
        "timestamp": "2026-04-27T10:00:01.000Z",
        "uuid": f"{session_id}-a1",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-sonnet-4-6",
            "id": f"msg_{session_id[:8]}",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        "version": "2.1.120",
    }))
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="lav_test_titles_"))
    project_dir = tmpdir / "-tmp-test-project-titles"
    project_dir.mkdir()
    db_path = tmpdir / "test.db"

    # Session A: only legacy summary
    _write_session(
        project_dir / "aaaaaaaa-1111-1111-1111-111111111111.jsonl",
        "aaaaaaaa-1111-1111-1111-111111111111",
        [{"type": "summary", "sessionId": "aaaaaaaa-1111-1111-1111-111111111111", "summary": "Legacy summary title"}],
        "what is this project about",
    )

    # Session B: legacy summary + ai-title (ai-title wins)
    _write_session(
        project_dir / "bbbbbbbb-2222-2222-2222-222222222222.jsonl",
        "bbbbbbbb-2222-2222-2222-222222222222",
        [
            {"type": "summary", "sessionId": "bbbbbbbb-2222-2222-2222-222222222222", "summary": "Old legacy summary"},
            {"type": "ai-title", "sessionId": "bbbbbbbb-2222-2222-2222-222222222222", "aiTitle": "AI generated title"},
        ],
        "fix the parser bug",
    )

    # Session C: ai-title + custom-title + summary (custom-title wins, regardless of order)
    _write_session(
        project_dir / "cccccccc-3333-3333-3333-333333333333.jsonl",
        "cccccccc-3333-3333-3333-333333333333",
        [
            {"type": "ai-title", "sessionId": "cccccccc-3333-3333-3333-333333333333", "aiTitle": "AI proposed"},
            {"type": "custom-title", "sessionId": "cccccccc-3333-3333-3333-333333333333", "customTitle": "user-pinned-title"},
            {"type": "summary", "sessionId": "cccccccc-3333-3333-3333-333333333333", "summary": "legacy fallback"},
        ],
        "rename this conversation",
    )

    # Session D: no title records at all → smart_title fallback from first prompt
    _write_session(
        project_dir / "dddddddd-4444-4444-4444-444444444444.jsonl",
        "dddddddd-4444-4444-4444-444444444444",
        [],
        "Spiegami come funziona il fallback",
    )

    conn = init_db(db_path)
    try:
        parse_project(project_dir, conn, full_reparse=True)
    finally:
        conn.commit()

    rows = dict(conn.execute(
        "SELECT session_id, summary FROM interactions ORDER BY session_id"
    ).fetchall())
    conn.close()

    expected = {
        "aaaaaaaa-1111-1111-1111-111111111111": "Legacy summary title",
        "bbbbbbbb-2222-2222-2222-222222222222": "AI generated title",
        "cccccccc-3333-3333-3333-333333333333": "user-pinned-title",
    }

    failures = []
    for sid, want in expected.items():
        got = rows.get(sid)
        marker = "OK " if got == want else "FAIL"
        print(f"  [{marker}] {sid[:8]}  want={want!r}  got={got!r}")
        if got != want:
            failures.append(sid)

    fallback_got = rows.get("dddddddd-4444-4444-4444-444444444444")
    fallback_ok = bool(fallback_got) and "Spiegami" in fallback_got
    marker = "OK " if fallback_ok else "FAIL"
    print(f"  [{marker}] dddddddd  fallback={fallback_got!r}")
    if not fallback_ok:
        failures.append("dddddddd")

    if failures:
        print(f"\nFAIL: {len(failures)} case(s) failed")
        return 1
    print("\nPASS: all 4 cases (legacy / ai-title / custom-title / fallback)")
    return 0


if __name__ == "__main__":
    sys.exit(run())

#!/usr/bin/env python3
"""
Classification model eval — compare multiple models on real interactions.

Usage:
    python3 tests/eval_classify.py                                    # 10 random, all models
    python3 tests/eval_classify.py --limit 5                          # 5 interactions
    python3 tests/eval_classify.py --models gpt-4.1-mini,phi4-mini    # subset of models
    python3 tests/eval_classify.py --session-id abc123,def456         # specific interactions
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure lav package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lav  # noqa: F401 — triggers .env loading
from lav import config
from lav.config import UNIFIED_DB_PATH
from lav.classifiers.openai_classifier import classify_interaction, prepare_messages_for_classification

OLLAMA_BASE_URL = "http://localhost:11434/v1"

DEFAULT_MODELS = ["gpt-4.1-mini", "phi4-mini", "qwen3:0.6b", "qwen3.5:0.8b"]

CLASSIFICATION_FIELDS = [
    "summary", "abstract", "process", "classification",
    "data_sensitivity", "sensitive_data_types", "topics", "people", "clients",
]


def _get_db() -> sqlite3.Connection:
    if not UNIFIED_DB_PATH.exists():
        print(f"ERROR: DB not found at {UNIFIED_DB_PATH}")
        sys.exit(1)
    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def _fetch_random_interactions(conn, limit=10, min_messages=5, session_ids=None):
    """Fetch interactions for eval. Random unless specific session_ids given."""
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        sql = f"""
            SELECT c.session_id, c.project_id, p.name AS project_name,
                   c.message_count
            FROM interactions c
            JOIN projects p ON c.project_id = p.id
            WHERE c.session_id IN ({placeholders})
              AND c.message_count >= ?
        """
        return conn.execute(sql, (*session_ids, min_messages)).fetchall()

    sql = """
        SELECT c.session_id, c.project_id, p.name AS project_name,
               c.message_count
        FROM interactions c
        JOIN projects p ON c.project_id = p.id
        WHERE c.message_count >= ?
        ORDER BY RANDOM()
        LIMIT ?
    """
    return conn.execute(sql, (min_messages, limit)).fetchall()


def _fetch_messages(conn, session_id, project_id):
    rows = conn.execute(
        "SELECT type, content FROM messages WHERE session_id = ? AND project_id = ? ORDER BY id",
        (session_id, project_id),
    ).fetchall()
    return [{"type": r["type"], "content": r["content"]} for r in rows]


def _make_client(model_name):
    """Create OpenAI client for model. Returns (client, is_ollama)."""
    import openai

    if model_name.startswith("gpt-"):
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            print(f"ERROR: OPENAI_API_KEY not set, cannot use {model_name}")
            sys.exit(1)
        return openai.OpenAI(api_key=api_key), False
    else:
        return openai.OpenAI(api_key="ollama", base_url=OLLAMA_BASE_URL), True


def _classify_with_model(messages, model_name):
    """Classify interaction with a specific model. Returns (result_dict, elapsed_secs, error_str)."""
    client, is_ollama = _make_client(model_name)

    # Temporarily override config to control strict vs json_object path
    orig_base_url = config.CLASSIFY_BASE_URL
    try:
        config.CLASSIFY_BASE_URL = OLLAMA_BASE_URL if is_ollama else ""
        t0 = time.time()
        result = classify_interaction(messages, client, model=model_name)
        elapsed = time.time() - t0
        return result, elapsed, None
    except Exception as e:
        elapsed = time.time() - t0
        return None, elapsed, str(e)
    finally:
        config.CLASSIFY_BASE_URL = orig_base_url


def _format_field(value):
    """Format a field value for the markdown table."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "(empty)"
    if isinstance(value, str):
        return value[:80] if len(value) > 80 else value
    return str(value)


def _generate_report(interactions_data, models, timestamp):
    """Generate markdown report."""
    lines = []
    lines.append(f"# Classification Eval — {timestamp}\n")

    # Setup
    lines.append("## Setup")
    lines.append(f"- Interactions: {len(interactions_data)} (random, min 5 messages)")
    lines.append(f"- Models: {', '.join(models)}")
    lines.append(f"- Ollama endpoint: {OLLAMA_BASE_URL}")
    lines.append("")

    # Compute summary stats
    baseline = models[0] if "gpt-4.1-mini" in models else None
    stats = {m: {"valid_json": 0, "errors": 0, "times": [], "cls_match": 0, "sens_match": 0}
             for m in models}

    for idata in interactions_data:
        for m in models:
            r = idata["results"].get(m)
            if r and r["result"] is not None:
                stats[m]["valid_json"] += 1
                stats[m]["times"].append(r["elapsed"])
                if baseline and m != baseline and idata["results"].get(baseline, {}).get("result"):
                    base_r = idata["results"][baseline]["result"]
                    if r["result"]["classification"] == base_r["classification"]:
                        stats[m]["cls_match"] += 1
                    if r["result"]["data_sensitivity"] == base_r["data_sensitivity"]:
                        stats[m]["sens_match"] += 1
            elif r:
                stats[m]["errors"] += 1

    n = len(interactions_data)

    # Summary table
    lines.append("## Summary Table\n")
    header = "| Metric |" + "|".join(f" {m} " for m in models) + "|"
    sep = "|--------|" + "|".join("---" for _ in models) + "|"
    lines.append(header)
    lines.append(sep)

    # Valid JSON
    row = "| Valid JSON |"
    for m in models:
        row += f" {stats[m]['valid_json']}/{n} |"
    lines.append(row)

    # Errors
    row = "| Errors |"
    for m in models:
        row += f" {stats[m]['errors']}/{n} |"
    lines.append(row)

    # Avg time
    row = "| Avg time (s) |"
    for m in models:
        t = stats[m]["times"]
        avg = f"{sum(t)/len(t):.1f}" if t else "—"
        row += f" {avg} |"
    lines.append(row)

    # Classification match vs baseline
    if baseline:
        row = "| Classification match vs baseline |"
        for m in models:
            if m == baseline:
                row += " — |"
            else:
                valid = stats[m]["valid_json"]
                row += f" {stats[m]['cls_match']}/{valid} |" if valid else " — |"
        lines.append(row)

        row = "| Sensitivity match vs baseline |"
        for m in models:
            if m == baseline:
                row += " — |"
            else:
                valid = stats[m]["valid_json"]
                row += f" {stats[m]['sens_match']}/{valid} |" if valid else " — |"
        lines.append(row)

    lines.append("")

    # Per-interaction results
    lines.append("## Per-interaction results\n")
    for idx, idata in enumerate(interactions_data, 1):
        sid = idata["session_id"][:8]
        proj = idata["project_name"]
        msgs = idata["message_count"]
        lines.append(f"### Interaction {idx}: {sid} (project: {proj}, msgs: {msgs})\n")

        # Sample text sent to models
        prepared = idata.get("prepared_text", "")
        if prepared:
            lines.append("<details>")
            lines.append("<summary>Sample sent to models (click to expand)</summary>\n")
            lines.append("```")
            lines.append(prepared)
            lines.append("```")
            lines.append("</details>\n")

        header = "| Field |" + "|".join(f" {m} " for m in models) + "|"
        sep = "|-------|" + "|".join("---" for _ in models) + "|"
        lines.append(header)
        lines.append(sep)

        for field in CLASSIFICATION_FIELDS:
            row = f"| {field} |"
            for m in models:
                r = idata["results"].get(m, {})
                if r.get("error"):
                    row += f" ERROR: {r['error'][:40]} |"
                elif r.get("result"):
                    row += f" {_format_field(r['result'].get(field, ''))} |"
                else:
                    row += " — |"
            lines.append(row)

        # Add timing row
        row = "| _time (s)_ |"
        for m in models:
            r = idata["results"].get(m, {})
            row += f" {r.get('elapsed', 0):.1f} |"
        lines.append(row)

        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Classification model eval")
    parser.add_argument("--limit", type=int, default=10, help="Number of random interactions (default: 10)")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS),
                        help=f"Comma-separated model list (default: {','.join(DEFAULT_MODELS)})")
    parser.add_argument("--session-id", default="",
                        help="Comma-separated session IDs (overrides --limit)")
    parser.add_argument("--min-messages", type=int, default=5,
                        help="Minimum messages per interaction (default: 5)")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    session_ids = [s.strip() for s in args.session_id.split(",") if s.strip()] if args.session_id else None

    print(f"Classification Eval")
    print(f"  Models: {', '.join(models)}")
    print(f"  DB: {UNIFIED_DB_PATH}")

    conn = _get_db()
    interactions = _fetch_random_interactions(
        conn, limit=args.limit, min_messages=args.min_messages, session_ids=session_ids
    )

    if not interactions:
        print("  No interactions found matching criteria.")
        conn.close()
        return

    print(f"  Interactions: {len(interactions)}")
    print()

    interactions_data = []

    for i, row in enumerate(interactions, 1):
        sid = row["session_id"]
        pid = row["project_id"]
        proj = row["project_name"] or "?"
        msg_count = row["message_count"] or 0

        print(f"[{i}/{len(interactions)}] {sid[:8]}  project={proj}  msgs={msg_count}")

        messages = _fetch_messages(conn, sid, pid)
        if not messages:
            print("  (no messages, skipping)")
            continue

        prepared_text = prepare_messages_for_classification(messages)

        idata = {
            "session_id": sid,
            "project_id": pid,
            "project_name": proj,
            "message_count": msg_count,
            "prepared_text": prepared_text,
            "results": {},
        }

        for model in models:
            print(f"  {model}...", end="", flush=True)
            result, elapsed, error = _classify_with_model(messages, model)
            if error:
                print(f" ERROR ({elapsed:.1f}s): {error}")
                idata["results"][model] = {"result": None, "elapsed": elapsed, "error": error}
            else:
                cls = result.get("classification", "?")
                sens = result.get("data_sensitivity", "?")
                print(f" OK ({elapsed:.1f}s) [{cls}/{sens}]")
                idata["results"][model] = {"result": result, "elapsed": elapsed, "error": None}

        interactions_data.append(idata)
        print()

    conn.close()

    # Generate report
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = _generate_report(interactions_data, models, timestamp)

    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(exist_ok=True)
    report_path = results_dir / f"eval_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")

    print(f"Report saved: {report_path}")


if __name__ == "__main__":
    main()

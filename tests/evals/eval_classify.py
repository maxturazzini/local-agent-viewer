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
import csv
import json
import os
import random
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# Ensure lav package is importable (repo root — this file lives at tests/evals/)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import lav  # noqa: F401 — triggers .env loading
from lav import config
from lav.config import UNIFIED_DB_PATH
from lav.classifiers.openai_classifier import (
    classify_interaction, prepare_messages_for_classification, full_scan_text,
    apply_sensitivity_floor,
)

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


def _classify_with_model(messages, model_name, session_key=None):
    """Classify interaction with a specific model. Returns (result_dict, elapsed_secs, error_str).

    A ``model@<backend>`` suffix overrides LAV_CLASSIFY_BACKEND for that run; the
    real model name is everything before the '@'. A ``foundry:<deployment>`` prefix
    routes the call through Azure AI Foundry (foundry backend, full single call).
    """
    backend = None
    if "@" in model_name:
        model_name, backend = model_name.split("@", 1)

    is_foundry = model_name.startswith("foundry:")
    if is_foundry:
        model_name = model_name.split(":", 1)[1]  # deployment name
        backend = backend or "foundry"

    # Temporarily override config to control strict vs json_object vs foundry path
    orig_base_url = config.CLASSIFY_BASE_URL
    orig_backend = config.CLASSIFY_BACKEND
    t0 = time.time()
    try:
        if is_foundry:
            from lav.classifiers.foundry.client import make_client
            client, is_ollama = make_client(model_name), False
        else:
            client, is_ollama = _make_client(model_name)
        config.CLASSIFY_BASE_URL = OLLAMA_BASE_URL if is_ollama else ""
        if backend:
            config.CLASSIFY_BACKEND = backend
        result = classify_interaction(messages, client, model=model_name, session_key=session_key)
        elapsed = time.time() - t0
        return result, elapsed, None
    except Exception as e:
        elapsed = time.time() - t0
        return None, elapsed, str(e)
    finally:
        config.CLASSIFY_BASE_URL = orig_base_url
        config.CLASSIFY_BACKEND = orig_backend


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


# ===========================================================================
# GOLD SCORING — evaluate models against human labels (ground truth), not a
# baseline model. Reads the golden-set CSV (internal_docs/golden_set_v1.csv).
# ===========================================================================

GOLD_DEFAULT = Path(__file__).resolve().parents[2] / "internal_docs" / "golden_set_v1.csv"


def _load_gold(path):
    """Read the golden-set CSV → {(session_id, project_id): {classification, data_sensitivity, verdict}}.

    Ground truth per row: gold_* if the annotator set it, else the AI label — an empty
    gold cell means "confirmed the AI". Only annotated rows (any gold_* or a verdict) count.
    """
    gold = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gc = (r.get("gold_classification") or "").strip()
            gs = (r.get("gold_data_sensitivity") or "").strip()
            verdict = (r.get("verdict") or "").strip()
            if not (gc or gs or verdict):
                continue  # not annotated yet
            try:
                pid = int(r["project_id"])
            except (KeyError, ValueError):
                continue
            gold[(r["session_id"], pid)] = {
                "classification": gc or (r.get("ai_classification") or "").strip(),
                "data_sensitivity": gs or (r.get("ai_data_sensitivity") or "").strip(),
                "verdict": verdict,
            }
    return gold


def _prf(pairs):
    """pairs = [(gold, pred), ...] → per-class P/R/F1/support, macro-F1, accuracy, confusion."""
    labels = sorted({g for g, _ in pairs} | {p for _, p in pairs})
    tp, fp, fn = Counter(), Counter(), Counter()
    confusion = defaultdict(Counter)
    correct = 0
    for g, p in pairs:
        confusion[g][p] += 1
        if g == p:
            tp[g] += 1
            correct += 1
        else:
            fp[p] += 1
            fn[g] += 1
    per = {}
    f1s = []
    for lab in labels:
        dp, dr = tp[lab] + fp[lab], tp[lab] + fn[lab]
        prec = tp[lab] / dp if dp else 0.0
        rec = tp[lab] / dr if dr else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        per[lab] = {"precision": prec, "recall": rec, "f1": f1, "support": tp[lab] + fn[lab]}
        if per[lab]["support"]:
            f1s.append(f1)
    return {
        "per_class": per,
        "macro_f1": sum(f1s) / len(f1s) if f1s else 0.0,
        "accuracy": correct / len(pairs) if pairs else 0.0,
        "confusion": confusion,
        "labels": labels,
        "n": len(pairs),
    }


def _pairs_for(interactions_data, model, field, gold):
    """Collect (gold, pred) pairs for one model/field; count rows dropped for model error/empty."""
    pairs, dropped = [], 0
    for idata in interactions_data:
        key = (idata["session_id"], idata["project_id"])
        g = gold.get(key, {}).get(field)
        if not g:
            continue
        res = idata["results"].get(model, {})
        pred = (res.get("result") or {}).get(field) if res.get("result") else None
        if pred:
            pairs.append((g, pred))
        else:
            dropped += 1
    return pairs, dropped


_SENS_ORD = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def _sens_directions(pairs):
    """Directional breakdown for data_sensitivity: the costly error is UNDER-escalation
    (a leak), not a raw mismatch — a cautious system prefers over-escalation. Returns
    (under, over, correct, severe); severe = a sensitive row (confidential/restricted)
    leaked down to public/internal."""
    under = over = correct = severe = 0
    for g, p in pairs:
        og, op = _SENS_ORD.get(g, 0), _SENS_ORD.get(p, 0)
        if op == og:
            correct += 1
        elif op < og:
            under += 1
            if og >= 2 and op <= 1:
                severe += 1
        else:
            over += 1
    return under, over, correct, severe


def _floored_sens_pairs(interactions_data, model, gold):
    """(gold, pred) for data_sensitivity AFTER applying the sensitivity floor to the
    model's RAW output — measured on the SAME outputs as the raw pairs, so the floor's
    effect is exact and noise-free (no separate stochastic run)."""
    pairs = []
    for idata in interactions_data:
        g = gold.get((idata["session_id"], idata["project_id"]), {}).get("data_sensitivity")
        if not g:
            continue
        res = idata["results"].get(model, {}).get("result")
        if not res:
            continue
        r2 = dict(res)
        apply_sensitivity_floor(r2, idata.get("prepared_text", ""), full_text=idata.get("full_scan", ""))
        pairs.append((g, r2["data_sensitivity"]))
    return pairs


def _gold_report(interactions_data, models, gold):
    """Markdown: per-model P/R/F1 vs gold for classification + data_sensitivity, with confusions."""
    lines = ["# Gold scoring — vs human labels\n"]
    lines.append("Ground truth = the `gold_*` columns of the golden set (empty = AI confirmed). "
                 "For `data_sensitivity`, recall on the higher levels matters most (missing sensitive data is the costly error).\n")
    for model in models:
        lines.append(f"## {model}\n")
        for field, title in (("classification", "Classification"), ("data_sensitivity", "Data sensitivity")):
            pairs, dropped = _pairs_for(interactions_data, model, field, gold)
            m = _prf(pairs)
            drop_note = f" · {dropped} dropped (model error/empty)" if dropped else ""
            lines.append(f"**{title}** — n={m['n']} · accuracy={m['accuracy']*100:.0f}% · macro-F1={m['macro_f1']:.2f}{drop_note}\n")
            lines.append("| class | precision | recall | F1 | support |")
            lines.append("|-------|-----------|--------|----|---------|")
            for lab in m["labels"]:
                pc = m["per_class"][lab]
                lines.append(f"| {lab} | {pc['precision']*100:.0f}% | {pc['recall']*100:.0f}% | {pc['f1']:.2f} | {pc['support']} |")
            lines.append("")
            conf = []
            for g in m["labels"]:
                wrong = {p: c for p, c in m["confusion"].get(g, {}).items() if p != g}
                if wrong:
                    conf.append(f"- gold **{g}** → " + ", ".join(f"{p}×{c}" for p, c in sorted(wrong.items(), key=lambda x: -x[1])))
            if conf:
                lines.append("Confusions (gold → predicted):")
                lines.extend(conf)
                lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Classification model eval")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap the number of interactions (random mode default: 10; "
                             "in --gold mode: screen on the first N gold sessions instead of all)")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS),
                        help=f"Comma-separated model list (default: {','.join(DEFAULT_MODELS)})")
    parser.add_argument("--session-id", default="",
                        help="Comma-separated session IDs (overrides --limit)")
    parser.add_argument("--min-messages", type=int, default=5,
                        help="Minimum messages per interaction (default: 5)")
    parser.add_argument("--gold", nargs="?", const=str(GOLD_DEFAULT), default=None,
                        help="Score against human gold labels from a golden-set CSV "
                             f"(bare flag uses {GOLD_DEFAULT.name}). Uses the CSV's annotated "
                             "sessions as the eval set and reports P/R/F1 per class vs the gold_* columns.")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    session_ids = [s.strip() for s in args.session_id.split(",") if s.strip()] if args.session_id else None

    gold = None
    if args.gold is not None:
        gold_path = Path(args.gold)
        if not gold_path.exists():
            print(f"ERROR: gold file not found: {gold_path}")
            sys.exit(1)
        gold = _load_gold(gold_path)
        if not gold:
            print(f"ERROR: no annotated rows in {gold_path}")
            sys.exit(1)
        session_ids = sorted({sid for sid, _ in gold})
        if args.limit:  # screening subset
            session_ids = session_ids[:args.limit]
        args.min_messages = 0  # exact gold sessions, no min filter
        note = f" (screening first {len(session_ids)})" if args.limit else ""
        print(f"  Gold: {len(gold)} annotated rows from {gold_path.name}; using {len(session_ids)} sessions{note}")

    print(f"Classification Eval")
    print(f"  Models: {', '.join(models)}")
    print(f"  DB: {UNIFIED_DB_PATH}")

    conn = _get_db()
    interactions = _fetch_random_interactions(
        conn, limit=(args.limit or 10), min_messages=args.min_messages, session_ids=session_ids
    )

    if not interactions:
        print("  No interactions found matching criteria.")
        conn.close()
        return

    if gold is not None:
        requested = set(session_ids)
        found_sids = {r["session_id"] for r in interactions}
        missing = requested - found_sids
        if missing:
            print(f"  ⚠ {len(missing)}/{len(requested)} requested gold sessions not in this DB (skipped) — "
                  f"run on the machine holding these sessions (e.g. minimacs).")

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
            "full_scan": full_scan_text(messages),
            "results": {},
        }

        for model in models:
            print(f"  {model}...", end="", flush=True)
            result, elapsed, error = _classify_with_model(messages, model, session_key=(sid, pid))
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

    if gold is not None:
        # Gold scoring goes first (it's the point of the run), then the baseline detail.
        report = _gold_report(interactions_data, models, gold) + "\n\n---\n\n" + report
        print()
        print("Gold scoring (vs human labels):")
        for model in models:
            for field in ("classification", "data_sensitivity"):
                pairs, _ = _pairs_for(interactions_data, model, field, gold)
                m = _prf(pairs)
                print(f"  {model:22s} {field:16s} acc {m['accuracy']*100:3.0f}%  macro-F1 {m['macro_f1']:.2f}  (n={m['n']})")
                if field == "data_sensitivity":
                    u, o, c, sv = _sens_directions(pairs)
                    print(f"  {'':22s}   ↳ RAW    leak {u} [severe {sv}] · over {o} · ok {c}")
                    fp = _floored_sens_pairs(interactions_data, model, gold)
                    fu, fo, fc, fsv = _sens_directions(fp)
                    fm = _prf(fp)
                    print(f"  {'':22s}   ↳ FLOOR  leak {fu} [severe {fsv}] · over {fo} · ok {fc}  · acc {fm['accuracy']*100:.0f}%")

        # Foundry per-call token usage → cost/task table (ground truth from API
        # usage, no Azure Monitor lag). Projected onto a full 17k-interaction batch.
        try:
            from lav.classifiers.foundry import classify as _fc
            if _fc.USAGE:
                from collections import defaultdict as _dd
                PRICE = {  # EUR / MTok (input, output); None = price not known yet
                    "gpt-5-mini": (0.25, 1.94), "gpt-oss-120b": (0.1317, 0.5266),
                    "deepseek-v4-flash": (0.16674, 0.44757), "gpt-5-1": (1.21, 9.66),
                }
                agg = _dd(lambda: {"n": 0, "p": 0, "c": 0, "r": 0})
                for u in _fc.USAGE:
                    a = agg[u["model"]]
                    a["n"] += 1; a["p"] += u["prompt"]; a["c"] += u["completion"]; a["r"] += u["reasoning"]
                print("\n  [foundry] token & cost per task (from API usage):")
                print(f"    {'model':18s} {'n':>3s} {'in/task':>8s} {'out/task':>9s} {'reas/task':>9s} {'€/task':>9s} {'€/17k':>8s}")
                for mdl in sorted(agg):
                    a = agg[mdl]; nn = a["n"] or 1
                    ip, op, rp = a["p"]/nn, a["c"]/nn, a["r"]/nn
                    pin, pout = PRICE.get(mdl, (None, None))
                    if pin is None:
                        ct, c17 = "n/a", "n/a"
                    else:
                        c = (ip*pin + op*pout)/1e6
                        ct, c17 = f"{c:.5f}", f"{c*17000:.1f}"
                    print(f"    {mdl:18s} {a['n']:>3d} {ip:>8.0f} {op:>9.0f} {rp:>9.0f} {ct:>9s} {c17:>8s}")
        except Exception:
            pass

    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(exist_ok=True)
    report_path = results_dir / f"eval_{timestamp}.md"
    report_path.write_text(report, encoding="utf-8")

    print(f"Report saved: {report_path}")


if __name__ == "__main__":
    main()

"""
Model pricing management for LocalAgentViewer.

Provides seed data, upsert, and query functions for the model_pricing table.
Costs are calculated at query time via JOIN — never materialized.
"""

import argparse
import sqlite3
from pathlib import Path

from lav.config import UNIFIED_DB_PATH

MODEL_PRICING_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_pricing (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    provider TEXT,
    input_price_per_mtok REAL NOT NULL,
    output_price_per_mtok REAL NOT NULL,
    cache_write_price_per_mtok REAL DEFAULT 0,
    cache_read_price_per_mtok REAL DEFAULT 0,
    from_date TEXT NOT NULL,
    to_date TEXT,
    notes TEXT,
    UNIQUE(model, from_date)
);
CREATE INDEX IF NOT EXISTS idx_pricing_model_date ON model_pricing(model, from_date);
"""

DEFAULT_PRICING = [
    # (model, provider, input, output, cache_write, cache_read, from_date)
    ("claude-opus-4-6", "anthropic", 5.00, 25.00, 6.25, 0.50, "2024-01-01"),
    ("claude-opus-4-5-20251101", "anthropic", 5.00, 25.00, 6.25, 0.50, "2024-01-01"),
    ("claude-opus-4-1-20250805", "anthropic", 15.00, 75.00, 18.75, 1.50, "2024-01-01"),
    ("claude-sonnet-4-6", "anthropic", 3.00, 15.00, 3.75, 0.30, "2024-01-01"),
    ("claude-sonnet-4-5-20250929", "anthropic", 3.00, 15.00, 3.75, 0.30, "2024-01-01"),
    ("claude-haiku-4-5-20251001", "anthropic", 1.00, 5.00, 1.25, 0.10, "2024-01-01"),
    ("gpt-5.2", "openai", 1.75, 14.00, 0, 0.875, "2024-01-01"),
    ("gpt-5.1-codex-max", "openai", 1.25, 10.00, 0, 0.625, "2024-01-01"),
    ("gpt-5.3-codex", "openai", 1.75, 14.00, 0, 0.875, "2024-01-01"),
    ("gpt-5-codex", "openai", 1.25, 10.00, 0, 0.625, "2024-01-01"),
]


def seed_default_pricing(conn: sqlite3.Connection):
    """Insert default pricing data (INSERT OR IGNORE — won't overwrite)."""
    for model, provider, inp, out, cw, cr, from_date in DEFAULT_PRICING:
        conn.execute(
            """INSERT OR IGNORE INTO model_pricing
               (model, provider, input_price_per_mtok, output_price_per_mtok,
                cache_write_price_per_mtok, cache_read_price_per_mtok, from_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (model, provider, inp, out, cw, cr, from_date),
        )
    conn.commit()


def upsert_pricing(conn: sqlite3.Connection, model: str, input_price: float,
                   output_price: float, from_date: str, provider: str = None,
                   cache_write: float = 0, cache_read: float = 0,
                   to_date: str = None, notes: str = None):
    """Add or update a pricing entry. Closes previous entry's to_date if needed."""
    # Close previous open entry for this model
    conn.execute(
        """UPDATE model_pricing SET to_date = ?
           WHERE model = ? AND to_date IS NULL AND from_date < ?""",
        (from_date, model, from_date),
    )
    conn.execute(
        """INSERT OR REPLACE INTO model_pricing
           (model, provider, input_price_per_mtok, output_price_per_mtok,
            cache_write_price_per_mtok, cache_read_price_per_mtok,
            from_date, to_date, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (model, provider, input_price, output_price, cache_write, cache_read,
         from_date, to_date, notes),
    )
    # Verify no overlapping ranges were created
    overlaps = conn.execute(
        """SELECT COUNT(*) FROM model_pricing a, model_pricing b
           WHERE a.model = ? AND b.model = ? AND a.id < b.id
           AND a.from_date < COALESCE(b.to_date, '9999-12-31')
           AND b.from_date < COALESCE(a.to_date, '9999-12-31')""",
        (model, model),
    ).fetchone()[0]
    if overlaps > 0:
        conn.rollback()
        raise ValueError(f"Overlapping date ranges detected for model '{model}'. Rolled back.")
    conn.commit()


def get_pricing(conn: sqlite3.Connection, model: str = None, active_only: bool = True):
    """List pricing entries."""
    clauses, params = [], []
    if model:
        clauses.append("model = ?")
        params.append(model)
    if active_only:
        clauses.append("to_date IS NULL")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    cursor = conn.execute(
        f"""SELECT id, model, provider, input_price_per_mtok, output_price_per_mtok,
                   cache_write_price_per_mtok, cache_read_price_per_mtok,
                   from_date, to_date, notes
            FROM model_pricing {where}
            ORDER BY model, from_date""",
        params,
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def main():
    """CLI entry point: lav-pricing list|add|seed"""
    parser = argparse.ArgumentParser(description="LAV model pricing management")
    sub = parser.add_subparsers(dest="command")

    # list
    list_cmd = sub.add_parser("list", help="List pricing entries")
    list_cmd.add_argument("--model", help="Filter by model name")
    list_cmd.add_argument("--all", dest="show_all", action="store_true",
                          help="Include expired entries")

    # add
    add_cmd = sub.add_parser("add", help="Add/update pricing entry")
    add_cmd.add_argument("--model", required=True)
    add_cmd.add_argument("--input", type=float, required=True, dest="input_price",
                         help="Input price per 1M tokens")
    add_cmd.add_argument("--output", type=float, required=True, dest="output_price",
                         help="Output price per 1M tokens")
    add_cmd.add_argument("--from-date", required=True, help="Start date (YYYY-MM-DD)")
    add_cmd.add_argument("--provider", help="Provider name (anthropic, openai, ...)")
    add_cmd.add_argument("--cache-write", type=float, default=0)
    add_cmd.add_argument("--cache-read", type=float, default=0)
    add_cmd.add_argument("--to-date", help="End date (YYYY-MM-DD, exclusive)")
    add_cmd.add_argument("--notes", help="Optional notes")

    # seed
    sub.add_parser("seed", help="Insert default pricing (won't overwrite)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    conn.executescript(MODEL_PRICING_SCHEMA)

    try:
        if args.command == "list":
            rows = get_pricing(conn, model=args.model,
                              active_only=not args.show_all)
            if not rows:
                print("No pricing entries found.")
                return
            fmt = "{:<35} {:<12} {:>8} {:>8} {:>8} {:>8}  {:<12} {:<12}"
            print(fmt.format("MODEL", "PROVIDER", "INPUT", "OUTPUT", "CW", "CR",
                            "FROM", "TO"))
            print("-" * 120)
            for r in rows:
                print(fmt.format(
                    r["model"][:35],
                    (r["provider"] or "")[:12],
                    f"${r['input_price_per_mtok']:.2f}",
                    f"${r['output_price_per_mtok']:.2f}",
                    f"${r['cache_write_price_per_mtok']:.2f}",
                    f"${r['cache_read_price_per_mtok']:.2f}",
                    r["from_date"],
                    r["to_date"] or "current",
                ))

        elif args.command == "add":
            upsert_pricing(
                conn, model=args.model, input_price=args.input_price,
                output_price=args.output_price, from_date=args.from_date,
                provider=args.provider, cache_write=args.cache_write,
                cache_read=args.cache_read, to_date=args.to_date,
                notes=args.notes,
            )
            print(f"Pricing added for {args.model} from {args.from_date}")

        elif args.command == "seed":
            seed_default_pricing(conn)
            print(f"Seeded {len(DEFAULT_PRICING)} default pricing entries.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

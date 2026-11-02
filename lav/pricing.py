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
    long_context_threshold INTEGER,
    lc_input REAL,
    lc_output REAL,
    lc_cache_write REAL,
    lc_cache_read REAL,
    UNIQUE(model, from_date)
);
CREATE INDEX IF NOT EXISTS idx_pricing_model_date ON model_pricing(model, from_date);
"""

# LAV-98: the high tier of a model, per single request. NULL threshold = no tier;
# a NULL lc_* keeps the base price of that component. queries._cost_exprs() is the
# only reader of these for pricing.
LONG_CONTEXT_COLUMNS = {
    "long_context_threshold": "INTEGER",
    "lc_input": "REAL",
    "lc_output": "REAL",
    "lc_cache_write": "REAL",
    "lc_cache_read": "REAL",
}

# Anthropic's rule, quoted: "A request's prompt length counts all of its input
# tokens, including cache reads and cache writes. Each request is priced on its
# own: a request over the threshold pays the higher prices even when part of its
# prompt is a cache hit." So strictly greater than, and the whole request moves.
# No lc price for the 1-hour cache write: it is 2x the input price of the tier
# applied (1.00 here).
# Every other Claude model is flat across the context window. OpenAI's tier
# (>272K, see the notes below) is deliberately not loaded: Codex rows in
# token_usage hold cumulative totals that are not single requests, so it would
# price them wrongly. The columns can hold it, no code change needed.
LONG_CONTEXT_TIERS = {
    "claude-haiku-5-5": {
        "long_context_threshold": 100000,
        "lc_input": 0.50,
        "lc_output": 2.50,
        "lc_cache_write": 0.625,
        "lc_cache_read": 0.05,
    },
}

DEFAULT_PRICING = [
    # (model, provider, input, output, cache_write, cache_read, from_date
    #  [, to_date [, notes]])  — the last two are optional.
    # NOTE: from_date is normally 2024-01-01. The seed only fills gaps for models
    # with no pricing at all (see seed_default_pricing). A model priced by hand
    # at its real release date is left alone; do NOT rely on the UNIQUE
    # (model, from_date) constraint to dedupe (LAV-76).
    # Opus 5.5 (2026-09-22) taglia del 20% input/output rispetto a Opus 5 e
    # porta il cache read a 0.05x dell'input (0.20) invece del solito 0.1x.
    ("claude-opus-5-5", "anthropic", 4.00, 20.00, 5.00, 0.20, "2026-09-22", None,
     "Opus 5.5 launch pricing (anthropic.com/claude-opus-5-5). Cache read = "
     "0.05x input, non il 0.1x standard. Fast mode 8/40 e batch 2/10 non modellati."),
    ("claude-opus-5", "anthropic", 5.00, 25.00, 6.25, 0.50, "2024-01-01"),
    ("claude-fable-5", "anthropic", 10.00, 50.00, 12.50, 1.00, "2024-01-01"),
    # Fable 5.1 succeeds Fable 5 at the same per-token input/output price, but
    # cuts cache_read to $0.25/MTok (vs $1.00 on Fable 5) — see claude-api skill.
    # from_date 2026-09-01: il 2026-09-02 lasciava fuori 20 chiamate del giorno 1.
    ("claude-fable-5-1", "anthropic", 10.00, 50.00, 12.50, 0.25, "2026-09-01"),
    ("claude-opus-4-8", "anthropic", 5.00, 25.00, 6.25, 0.50, "2024-01-01"),
    ("claude-opus-4-7", "anthropic", 5.00, 25.00, 6.25, 0.50, "2024-01-01"),
    ("claude-opus-4-6", "anthropic", 5.00, 25.00, 6.25, 0.50, "2024-01-01"),
    ("claude-opus-4-5-20251101", "anthropic", 5.00, 25.00, 6.25, 0.50, "2024-01-01"),
    ("claude-opus-4-1-20250805", "anthropic", 15.00, 75.00, 18.75, 1.50, "2024-01-01"),
    # Sonnet 5: l'aumento a 3/15 previsto per il 2026-09-01 e' stato annullato,
    # il prezzo intro 2/10 e' diventato standard (platform.claude.com pricing).
    # Una riga sola, aperta: le due righe storicizzate precedenti facevano
    # pagare Sonnet 5 il 50% in piu' da settembre in avanti.
    ("claude-sonnet-5", "anthropic", 2.00, 10.00, 2.50, 0.20, "2024-01-01", None,
     "Sonnet 5 standard 2/10. L'aumento a 3/15 del 2026-09-01 e' stato annullato "
     "da Anthropic: il prezzo intro e' diventato standard."),
    # Sonnet 5.5: due righe, le release notes abbassano il cache read a 0.05x
    # dell'input dal 2026-10-07. Il resto del listino non cambia.
    ("claude-sonnet-5-5", "anthropic", 2.00, 10.00, 2.50, 0.20, "2026-09-29", "2026-10-07",
     "Sonnet 5.5, platform.claude.com/docs pricing 2026-10-04. Cache write 1h "
     "calcolato a 2x input; batch non modellato."),
    ("claude-sonnet-5-5", "anthropic", 2.00, 10.00, 2.50, 0.10, "2026-10-07", None,
     "Sonnet 5.5, cache read abbassato da 0.20 a 0.10 (0.05x input) il 2026-10-07, "
     "platform.claude.com release notes. Cache write 1h calcolato a 2x input; batch "
     "non modellato."),
    ("claude-sonnet-4-6", "anthropic", 3.00, 15.00, 3.75, 0.30, "2024-01-01"),
    ("claude-sonnet-4-5-20250929", "anthropic", 3.00, 15.00, 3.75, 0.30, "2024-01-01"),
    ("claude-haiku-4-5-20251001", "anthropic", 1.00, 5.00, 1.25, 0.10, "2024-01-01"),
    # Haiku 5.5: due fasce per lunghezza del prompt. Qui i prezzi base (<=100K),
    # la fascia alta sta in LONG_CONTEXT_TIERS. Cache write 5m 1.25x, cache read
    # 0.1x dell'input.
    ("claude-haiku-5-5", "anthropic", 0.10, 0.50, 0.125, 0.01, "2026-10-01", None,
     "Haiku 5.5, prompt <=100K token. Oltre 100K fascia alta 0.50/2.50, cache write "
     "0.625, cache read 0.05 (LONG_CONTEXT_TIERS). Cache write 1h a 2x l'input della "
     "fascia applicata."),
    # cache_read = official cached-input (10% of input) — LAV-74 alignment
    ("gpt-5.2", "openai", 1.75, 14.00, 0, 0.175, "2024-01-01"),
    ("gpt-5.1-codex-max", "openai", 1.25, 10.00, 0, 0.125, "2024-01-01"),
    ("gpt-5.3-codex", "openai", 1.75, 14.00, 0, 0.175, "2024-01-01"),
    ("gpt-5-codex", "openai", 1.25, 10.00, 0, 0.125, "2024-01-01"),
    # LAV-74: newer Codex surfaces (Desktop / VS Code / Work) — official
    # developers.openai.com/api/docs/pricing (input / output / cached-input $/Mtok).
    ("gpt-5.4", "openai", 2.50, 15.00, 0, 0.25, "2024-01-01"),
    ("gpt-5.5", "openai", 5.00, 30.00, 0, 0.50, "2024-01-01"),
    # Famiglia GPT-5.6: corretta il 2026-09-22 sul listino ufficiale, verificato
    # sul payload della pagina e ricontrollato contro le tabelle batch (meta')
    # e fast mode (doppio), che tornano entrambe. I valori precedenti
    # (5/30, 2.50/15, 1.00/6.00) non hanno mai corrisposto al reale.
    ("gpt-5.6-sol", "openai", 4.00, 20.00, 5.00, 0.40, "2024-01-01", None,
     "Prezzo promozionale 4/20, garantito almeno fino al 2026-11-21: "
     "ricontrollare dopo quella data, potrebbe tornare a 5/30."),
    ("gpt-5.6-terra", "openai", 2.00, 12.00, 2.50, 0.20, "2024-01-01"),
    ("gpt-5.6-luna", "openai", 0.20, 1.20, 0.25, 0.02, "2024-01-01"),
    # GPT-6 Astra, uscita 2026-09-03: from_date reale, non il 2024-01-01 di
    # comodo, perche' prima non esisteva. Cached input 1.00/MTok.
    # ATTENZIONE, cambio di regola rispetto ai modelli OpenAI piu' vecchi: dal
    # 2026-09-22 il cache write NON e' piu' 0. Su gpt-6-* e gpt-5.6-* il listino
    # ufficiale ha una colonna "Cache writes" con un prezzo vero; su gpt-5.5 e
    # precedenti quella colonna e' "-", e li' lo 0 resta giusto. Oggi i
    # transcript Codex riportano cache_creation_tokens = 0, quindi il prezzo non
    # moltiplica nulla: e' a listino perche' sia corretto se inizieranno ad
    # arrivare. Fast mode 2x, batch/flex 0.5x e la fascia oltre 272K token
    # (20/75) non sono modellati qui.
    ("gpt-6-astra", "openai", 10.00, 50.00, 12.50, 1.00, "2026-09-03", None,
     "GPT-6 Astra launch pricing (openai.com/index/gpt-6-astra, 2026-09-06). "
     "Fast mode 2x, batch/flex 0.5x, oltre 272K token 20/75: non modellati."),
    ("gpt-6-sol", "openai", 2.00, 10.00, 2.50, 0.20, "2026-09-03", None,
     "GPT-6 Sol (developers.openai.com/api/docs/pricing). Long context >272K "
     "4/15, fast 2x, batch/flex 0.5x: non modellati."),
    # GPT-6.1 Sol: stesso listino di gpt-6-sol, cambia solo il cached input
    # (0.20 -> 0.10). Cache write 2.50 da fonti secondarie, non dalla pagina
    # ufficiale. from_date = primo dato in LAV, la data di uscita e' incerta.
    ("gpt-6.1-sol", "openai", 2.00, 10.00, 2.50, 0.10, "2026-10-01", None,
     "GPT-6.1 Sol (openai.com/index/introducing-gpt-6-1-sol). Cache write 2.50 "
     "non verificato sul listino ufficiale. >272K 2x input/cache 1.5x output, "
     "fast 2x, batch/flex 0.5x: non modellati."),
    ("gpt-6.1-terra", "openai", 1.00, 5.00, 1.25, 0.05, "2026-10-26", None,
     "GPT-6.1 Terra, developers.openai.com/api/docs/pricing, verificato 2026-11-02. "
     "Sopra 272K token 2/7.50, cache write 12h 2.50, fast e batch non modellati."),
    ("gpt-6-luna", "openai", 0.10, 0.50, 0.125, 0.01, "2026-09-03", None,
     "GPT-6 Luna (developers.openai.com/api/docs/pricing). Long context >272K "
     "0.20/0.75, fast 2x, batch/flex 0.5x: non modellati."),
    # LAV-76: hidden Codex slug (models_cache.json: "Automatic approval review
    # model for Codex"), used when approval_policy=never + reviewer=auto_review.
    # No public OpenAI listing; spend rides the ChatGPT plan, not per-token.
    # Deliberately zero so it doesn't sit in the missing-price warning forever.
    ("codex-auto-review", "openai", 0, 0, 0, 0, "2024-01-01", None,
     "Codex internal auto-approval review model (hidden slug, no public "
     "listing). Zero on purpose: billed on the ChatGPT plan, not per-token."),
]


def seed_default_pricing(conn: sqlite3.Connection):
    """Insert default pricing for models that have no pricing row at all.

    LAV-76: this used to be ``INSERT OR IGNORE``, which only skipped on the
    ``UNIQUE(model, from_date)`` constraint. A model already priced by hand at
    its real release date (e.g. gpt-5.4 from 2026-03-05) did not collide with
    the seed's 2024-01-01, so a *second open-ended row* was inserted — and the
    price join in queries.py matches both, doubling tokens and cost. init_db()
    calls this on every parse, so the damage came back within minutes of any
    manual cleanup.

    The gate is now per *model*: a model with any existing row is left
    untouched, which is what OR IGNORE was meant to express.
    """
    priced = {row[0] for row in conn.execute("SELECT DISTINCT model FROM model_pricing")}
    for entry in DEFAULT_PRICING:
        model, provider, inp, out, cw, cr, from_date = entry[:7]
        to_date = entry[7] if len(entry) > 7 else None
        notes = entry[8] if len(entry) > 8 else None
        # Snapshot taken before the loop, so multi-row models (historicised
        # pricing) seed all of their rows rather than just the first.
        if model in priced:
            continue
        conn.execute(
            """INSERT INTO model_pricing
               (model, provider, input_price_per_mtok, output_price_per_mtok,
                cache_write_price_per_mtok, cache_read_price_per_mtok,
                from_date, to_date, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (model, provider, inp, out, cw, cr, from_date, to_date, notes),
        )
    conn.commit()


def migrate_long_context_columns(conn: sqlite3.Connection):
    """LAV-98: add the long-context columns to a model_pricing that lacks them.

    Idempotent, column by column. Lives here rather than with the other migrations
    in parsers/jsonl.py because the lav-pricing CLI and upsert_pricing() need it
    too, and they do not go through init_db().
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(model_pricing)")}
    for col, sql_type in LONG_CONTEXT_COLUMNS.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE model_pricing ADD COLUMN {col} {sql_type}")
    conn.commit()


def apply_long_context_tiers(conn: sqlite3.Connection):
    """LAV-98: load LONG_CONTEXT_TIERS onto the rows of their models.

    Runs on fresh and existing DBs alike, because the seed never touches an
    existing row (LAV-76). Only rows with a NULL threshold, and no lc_* already
    there is replaced, so a value set by hand is never overwritten.
    """
    for model, tier in LONG_CONTEXT_TIERS.items():
        conn.execute(
            """UPDATE model_pricing
               SET long_context_threshold = ?,
                   lc_input = COALESCE(lc_input, ?),
                   lc_output = COALESCE(lc_output, ?),
                   lc_cache_write = COALESCE(lc_cache_write, ?),
                   lc_cache_read = COALESCE(lc_cache_read, ?)
               WHERE model = ? AND long_context_threshold IS NULL""",
            [tier.get(col) for col in LONG_CONTEXT_COLUMNS] + [model],
        )
    conn.commit()


def ensure_pricing_overlap_guard(conn: sqlite3.Connection) -> bool:
    """Enforce "at most one open-ended pricing row per model" (LAV-76).

    Structural backstop: the price join in queries.py multiplies rows when two
    pricing entries match the same timestamp, so a model with two open-ended
    rows silently doubles both tokens and cost. This index makes that state
    unrepresentable for every write path (seed, CLI, MCP, API).

    Legitimate historicisation still passes: only the *current* row is
    open-ended, older ones carry a to_date.

    Returns True if the index is in place. On a DB that still holds duplicates
    the CREATE fails — report the offending models and carry on rather than
    blocking startup on a pre-existing data problem.
    """
    try:
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_pricing_one_open_per_model
               ON model_pricing(model) WHERE to_date IS NULL"""
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        dupes = conn.execute(
            """SELECT model, COUNT(*) FROM model_pricing WHERE to_date IS NULL
               GROUP BY model HAVING COUNT(*) > 1"""
        ).fetchall()
        listed = ", ".join(f"{m} ({n} open rows)" for m, n in dupes)
        print(f"  WARNING: overlapping pricing rows — cost and token counts are "
              f"double-counted for: {listed}")
        print("  Keep one open row per model (the annotated one), then re-run "
              "to install the guard.")
        return False


def upsert_pricing(conn: sqlite3.Connection, model: str, input_price: float,
                   output_price: float, from_date: str, provider: str = None,
                   cache_write: float = 0, cache_read: float = 0,
                   to_date: str = None, notes: str = None,
                   long_context_threshold: int = None, lc_input: float = None,
                   lc_output: float = None, lc_cache_write: float = None,
                   lc_cache_read: float = None):
    """Add or update a pricing entry. Closes previous entry's to_date if needed.

    LAV-98: a tier field left None takes the value of the row this call closes or
    replaces, so re-pricing a model does not silently lose its tier.
    """
    migrate_long_context_columns(conn)  # no init_db() on the CLI and MCP paths
    tier_cols = ", ".join(LONG_CONTEXT_COLUMNS)
    # Read before the UPDATE below, which closes the row. The three shapes: same
    # from_date (REPLACE, UNIQUE(model, from_date)); an older open row (closed
    # below); a newer open row when this one is open too (REPLACE, LAV-76 index).
    superseded = conn.execute(
        f"""SELECT {tier_cols} FROM model_pricing
            WHERE model = ? AND (from_date = ?
                OR (to_date IS NULL AND (from_date < ? OR ? IS NULL)))
            ORDER BY from_date = ? DESC LIMIT 1""",
        (model, from_date, from_date, to_date, from_date),
    ).fetchone() or (None,) * len(LONG_CONTEXT_COLUMNS)
    given = (long_context_threshold, lc_input, lc_output, lc_cache_write, lc_cache_read)
    tier = [new if new is not None else old for new, old in zip(given, superseded)]
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
            from_date, to_date, notes, long_context_threshold,
            lc_input, lc_output, lc_cache_write, lc_cache_read)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (model, provider, input_price, output_price, cache_write, cache_read,
         from_date, to_date, notes, *tier),
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
    # LAV-98: /api/pricing, `lav pricing list` and MCP read on query_only connections
    # and cannot migrate, so a DB no init_db() has reached yet lists the tier as NULL
    # instead of raising.
    have = {row[1] for row in conn.execute("PRAGMA table_info(model_pricing)")}
    tier_cols = ", ".join(c if c in have else f"NULL AS {c}" for c in LONG_CONTEXT_COLUMNS)

    cursor = conn.execute(
        f"""SELECT id, model, provider, input_price_per_mtok, output_price_per_mtok,
                   cache_write_price_per_mtok, cache_read_price_per_mtok,
                   from_date, to_date, notes, {tier_cols}
            FROM model_pricing {where}
            ORDER BY model, from_date""",
        params,
    )
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def add_long_context_args(parser: argparse.ArgumentParser):
    """The tier options shared by `lav-pricing add` and `lav pricing add`.

    Each dest is a LONG_CONTEXT_COLUMNS key, see long_context_kwargs().
    """
    parser.add_argument("--long-context-threshold", type=int,
                        help="Prompt tokens above which a request pays the lc_* prices "
                             "(not given: inherited from the row closed or replaced)")
    parser.add_argument("--lc-input", type=float, help="Input price/Mtok over the threshold")
    parser.add_argument("--lc-output", type=float, help="Output price/Mtok over the threshold")
    parser.add_argument("--lc-cache-write", type=float,
                        help="5-minute cache write price/Mtok over the threshold")
    parser.add_argument("--lc-cache-read", type=float,
                        help="Cache read price/Mtok over the threshold")


def long_context_kwargs(args: argparse.Namespace) -> dict:
    """The upsert_pricing() tier arguments from add_long_context_args() options."""
    return {col: getattr(args, col) for col in LONG_CONTEXT_COLUMNS}


def _format_tier(row: dict) -> str:
    """Tier of a get_pricing() row for the list table, blank if it has none."""
    if row["long_context_threshold"] is None:
        return ""
    prices = "/".join("-" if row[c] is None else f"{row[c]:g}" for c in
                      ("lc_input", "lc_output", "lc_cache_write", "lc_cache_read"))
    return f">{row['long_context_threshold']} {prices}"


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
    add_long_context_args(add_cmd)

    # seed
    sub.add_parser("seed", help="Insert default pricing (won't overwrite)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    conn = sqlite3.connect(str(UNIFIED_DB_PATH))
    conn.executescript(MODEL_PRICING_SCHEMA)
    migrate_long_context_columns(conn)  # LAV-98
    apply_long_context_tiers(conn)
    ensure_pricing_overlap_guard(conn)  # LAV-76

    try:
        if args.command == "list":
            rows = get_pricing(conn, model=args.model,
                              active_only=not args.show_all)
            if not rows:
                print("No pricing entries found.")
                return
            fmt = "{:<35} {:<12} {:>8} {:>8} {:>8} {:>8}  {:<12} {:<12} {}"
            print(fmt.format("MODEL", "PROVIDER", "INPUT", "OUTPUT", "CW", "CR",
                            "FROM", "TO", "LONG CONTEXT (>TOKENS IN/OUT/CW/CR)"))
            print("-" * 140)
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
                    _format_tier(r),
                ))

        elif args.command == "add":
            upsert_pricing(
                conn, model=args.model, input_price=args.input_price,
                output_price=args.output_price, from_date=args.from_date,
                provider=args.provider, cache_write=args.cache_write,
                cache_read=args.cache_read, to_date=args.to_date,
                notes=args.notes, **long_context_kwargs(args),
            )
            print(f"Pricing added for {args.model} from {args.from_date}")

        elif args.command == "seed":
            seed_default_pricing(conn)
            apply_long_context_tiers(conn)  # LAV-98: a model just seeded has no tier yet
            print(f"Seeded {len(DEFAULT_PRICING)} default pricing entries.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

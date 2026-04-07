# Cost Intelligence — Design Spec

**Date:** 2026-04-07
**Status:** Alpha feature
**Approach:** Modular query functions + composite endpoint (Approach 2)

## Goal

Help users understand how their work patterns impact token costs. Two levels of analysis:

1. **Aggregate (DB-wide)** — New "Cost Intelligence" dashboard tab showing behavioral patterns, task-type costs, and efficiency metrics across all sessions.
2. **Per-session** — New "Cost Profile" tab on the interaction detail page showing cumulative cost timeline and message-level token breakdown.

All analysis is SQLite-only — no new tables, no new dependencies. Computed server-side via SQL queries.

## Design Principles

- **Graceful degradation:** Works without AI classification. Heuristic-based complexity buckets (quick/medium/deep) are always available. Classification data (`interaction_metadata`) enriches with task-type breakdowns when present.
- **Alpha banner:** Clearly marked as alpha. Suggests running `lav-classify` for deeper analysis.
- **Consistent with existing architecture:** Modular functions in `queries.py`, composed in `server.py`, rendered by vanilla JS + Chart.js frontend.
- **Standard 4D filtering:** All queries accept project, user, host, source, and date range filters.

## Query Functions (queries.py)

### `get_session_cost_profile(db, session_id, project_id)`

Per-conversation cost analysis. Returns:

- **`summary`**: total `cost_usd`, token breakdown (input/output/cache_write/cache_read), `duration_minutes` (first-to-last timestamp), `api_calls` count, `models` list used.
- **`timeline`**: ordered list of each API call from `token_usage` — `timestamp`, `model`, `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`, `cost_usd`, `cumulative_cost`.
- **`phases`**: conversation split into thirds (early/mid/late) by API call count — `cost_usd` and `pct` (percentage of total cost) per phase.

Data source: `token_usage` JOIN `model_pricing` filtered by `session_id` and `project_id`.

### `get_work_pattern_stats(db, filters)`

Behavioral analysis across sessions. Returns:

- **`by_hour`**: for each hour 0-23 — `hour`, `session_count`, `total_cost`, `avg_cost`, `total_tokens`.
  - SQL: `strftime('%H', timestamp)` grouping on `token_usage`.
- **`by_day_of_week`**: for each weekday — `day` (0=Sun, 6=Sat from `strftime('%w')`), `day_name` (human label), `session_count`, `total_cost`, `avg_cost`. Frontend reorders to Mon-Sun for display.
  - SQL: `strftime('%w', timestamp)` grouping.
- **`by_complexity_bucket`**: sessions bucketed by API call count:
  - "quick": <10 calls
  - "medium": 10-50 calls
  - "deep": 50+ calls
  - Per bucket: `count`, `avg_cost`, `avg_cache_hit_rate`, `total_cost`.
  - SQL: subquery counting calls per session, then CASE bucketing.
- **`by_session_length`**: bins of message count ranges vs avg cost — shows correlation between conversation length and cost. Bins: 1-5, 6-15, 16-30, 31-60, 60+.

### `get_task_type_costs(db, filters)`

Classification-enriched analysis. Returns:

- **`data`**: list of `{ task_type, session_count, avg_cost, total_cost, avg_tokens }` grouped by `interaction_metadata.task_type`. Empty list if no classification data.
- **`classified_ratio`**: string like `"142/300"` — classified sessions / total sessions in filter range.
- **`has_classification`**: boolean — true if any `interaction_metadata` rows exist for the filtered set.

SQL: LEFT JOIN `interaction_metadata` on `(session_id, project_id)` with `interactions`, then JOIN `token_usage` for cost aggregation.

### `get_efficiency_metrics(db, filters)`

Cost efficiency trends. Returns:

- **`cache_trend`**: daily series — `date`, `cache_hit_rate` (cache_read / (input + cache_read + cache_creation) * 100).
- **`cost_per_call_trend`**: daily series — `date`, `avg_cost_per_call`.
- **`model_efficiency`**: per model — `model`, `avg_cost_per_session`, `avg_tokens_per_session`, `session_count`.
- **`source_comparison`**: per source — `source`, `avg_cost_per_session`, `session_count`, `avg_cache_hit_rate`. Only populated if multiple sources exist.

### `generate_insights(work_patterns, task_type_costs, efficiency)`

Takes the output of the three aggregate functions and produces a list of 6-8 human-readable insight strings. Server-side heuristic engine.

Insight pool (picks most relevant based on data availability):

1. **Expensive hour:** "Your most expensive hour is {H}:00 — {X}x your daily average"
2. **Deep session cost:** "Deep sessions (50+ calls) cost {X}x more than quick tasks but only {Y}x the output"
3. **Cache trend:** "Cache hit rate improved from {X}% to {Y}% over the last 30 days" (or "declined")
4. **Source comparison:** "{Source A} sessions cost {X}% less than {Source B} for comparable session lengths" (only if multiple sources)
5. **Efficiency trend:** "Your cost per API call has {increased/decreased} {X}% this month vs last month"
6. **Token type dominance:** "{X}% of your spend goes to {output/input} tokens — {actionable suggestion}"
7. **Day-of-week outlier:** "{Day} is your most expensive day (${X} avg) — {Y}x your weekly average"
8. **Complexity sweet spot:** "Sessions between {range} API calls have the best cache hit rate ({X}%) — sweet spot for cost efficiency"

Capped at 8 max. Each insight has a threshold to qualify (e.g., source comparison only fires if delta > 15%).

## API

### `GET /api/cost-intelligence`

New endpoint. Composes `get_work_pattern_stats`, `get_task_type_costs`, `get_efficiency_metrics`, and `generate_insights`.

**Parameters** (same as `/api/data`):
- `project` — project_id
- `user` — user_id
- `host` — host_id
- `start` — YYYY-MM-DD
- `end` — YYYY-MM-DD
- `client` — source filter

**Response:**
```json
{
  "work_patterns": {
    "by_hour": [{ "hour": 0, "session_count": 5, "total_cost": 12.40, "avg_cost": 2.48, "total_tokens": 125000 }, ...],
    "by_day_of_week": [{ "day": 0, "day_name": "Sunday", "session_count": 3, "total_cost": 8.20, "avg_cost": 2.73 }, ...],
    "by_complexity_bucket": [{ "bucket": "quick", "count": 80, "avg_cost": 0.45, "avg_cache_hit_rate": 62.3, "total_cost": 36.00 }, ...],
    "by_session_length": [{ "range": "1-5", "count": 40, "avg_cost": 0.30 }, ...]
  },
  "task_type_costs": {
    "data": [{ "task_type": "bug_fix", "session_count": 25, "avg_cost": 1.20, "total_cost": 30.00, "avg_tokens": 45000 }, ...],
    "classified_ratio": "142/300",
    "has_classification": true
  },
  "efficiency": {
    "cache_trend": [{ "date": "2026-03-01", "cache_hit_rate": 45.2 }, ...],
    "cost_per_call_trend": [{ "date": "2026-03-01", "avg_cost_per_call": 0.07 }, ...],
    "model_efficiency": [{ "model": "claude-opus-4-6", "avg_cost_per_session": 3.20, "avg_tokens_per_session": 85000, "session_count": 120 }, ...],
    "source_comparison": [{ "source": "claude_code", "avg_cost_per_session": 2.80, "session_count": 200, "avg_cache_hit_rate": 68.5 }, ...]
  },
  "insights": [
    "Your most expensive hour is 15:00 — 2.3x your daily average",
    "Cache hit rate improved from 45% to 72% over the last 30 days",
    "70% of your spend goes to output tokens — consider structured response formats"
  ],
  "alpha": true
}
```

**Auth:** Read-only — uses `LAV_READ_API_KEY` gating if set, same as `/api/data`.

### Enriched `GET /api/interaction/<session_id>`

Existing endpoint gains a `cost_profile` field in its response by calling `get_session_cost_profile()`:

```json
{
  "...existing fields...",
  "cost_profile": {
    "summary": {
      "cost_usd": 2.40,
      "duration_minutes": 23,
      "api_calls": 34,
      "models": ["claude-opus-4-6"],
      "tokens": { "input": 120000, "output": 45000, "cache_creation": 8000, "cache_read": 95000 }
    },
    "timeline": [
      { "timestamp": "2026-04-07T10:00:00", "model": "claude-opus-4-6", "input_tokens": 1200, "output_tokens": 450, "cache_creation_tokens": 0, "cache_read_tokens": 800, "cost_usd": 0.07, "cumulative_cost": 0.07 },
      ...
    ],
    "phases": {
      "early": { "cost": 0.80, "pct": 33 },
      "mid": { "cost": 1.10, "pct": 46 },
      "late": { "cost": 0.50, "pct": 21 }
    }
  }
}
```

Field is `null` if no `token_usage` rows exist for the session.

## Frontend

### Cost Intelligence Tab (dashboard.html)

New tab in the dashboard navigation, after the existing tabs.

**Alpha banner (top):**
> "Cost Intelligence (Alpha) — This analysis uses heuristics to identify work patterns. Run `lav-classify` for deeper task-type breakdowns. More insights coming in future releases."

Subtle info-bar style (blue/gray background), dismissible.

**Section 1: Work Patterns**
- **Hour heatmap**: horizontal bar chart, 24 bars (hours 0-23), color intensity by cost. X-axis = cost.
- **Day of week**: 7 vertical bars (Mon-Sun), showing cost + session count. Dual-axis or labels.
- **Complexity buckets**: 3 cards — Quick / Medium / Deep — each showing: session count, avg cost, avg cache hit rate.

**Section 2: Task Type Costs** (conditional)
- Shown only if `has_classification` is true.
- Horizontal bar chart: task types on Y-axis, avg cost on X-axis. Session count as label.
- If `classified_ratio` is low (<50%), muted note: "Based on {n} of {total} sessions — classify more for accuracy."
- If no classification data: section hidden entirely, banner already covers the suggestion.

**Section 3: Efficiency Metrics**
- **Cache trend**: line chart, daily cache hit rate % over time.
- **Cost per call trend**: line chart, daily avg cost per API call over time.
- **Model efficiency**: table — Model | Avg Cost/Session | Avg Tokens/Session | Sessions.
- **Source comparison**: bar chart — one bar per source (claude_code, codex_cli, etc.) showing avg cost per session. Only shown if >1 source exists.

**Section 4: Key Insights**
- Card/panel with 6-8 bullet points from `insights` array.
- Each insight is a standalone sentence with embedded numbers.
- Styled as a summary panel (light background, readable font).

**Filters:** Uses the same filter bar as other dashboard tabs (project, user, host, source, date range). Calls `/api/cost-intelligence` with selected filters on change.

### Cost Profile Tab (interaction detail page)

New tab on the interaction detail view (interactions.html detail modal/page). Tab label: **"Cost"**.

**Summary cards row:**
- Total Cost | Duration | API Calls | Model(s) | Avg Cost/Call
- Same card style as dashboard hero cards.

**Cumulative cost chart:**
- Line chart, X-axis = conversation timeline (timestamps), Y-axis = cumulative cost in $.
- Points colored by model if multiple models used.
- Steep slopes indicate token-heavy moments.

**Message-level detail table:**
- Columns: # | Timestamp | Model | Input | Output | Cache Write | Cache Read | Cost | Cumulative
- Sortable by any column.
- Phase indicator as subtle left-border color (early=blue, mid=amber, late=green) on rows.

**Empty state:** If `cost_profile` is null (no token data), tab shows: "No token data available for this session."

## Files to Modify

| File | Change |
|------|--------|
| `lav/queries.py` | Add 4 query functions + `generate_insights` |
| `lav/server.py` | Add `/api/cost-intelligence` endpoint, enrich `/api/interaction/<id>` |
| `lav/static/dashboard.html` | Add "Cost Intelligence" tab with 4 sections |
| `lav/static/interactions.html` | Add "Cost" tab to interaction detail view |

No new files. No new dependencies. No schema changes.

## Out of Scope (Future)

- Cost forecasting / budget alerts
- Per-file or per-tool cost attribution
- Comparison between users
- Export/report generation
- Real-time cost tracking during active sessions

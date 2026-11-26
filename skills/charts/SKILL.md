---
name: charts
description: How to embed interactive charts, KPI stat tiles, sortable tables, step timelines, and callouts directly inside Flock chats using fenced JSON visual blocks.
---

# Flock Visuals v1: Inline Charts, Tables & KPI Blocks

When numbers, trends, comparisons, distributions or timelines are clearer visually than in prose, emit a fenced code block with pure JSON. Flock renders these natively: fast, interactive, and CSP-safe.

## 1. Charts (```` ```chart ```` or ```` ```compare ````)

Supported `type` values: `bar`, `hbar`, `line`, `area`, `scatter`, `donut`, `progress`.

### Multi-series / time-series (bar, line, area)
```chart
{
  "type": "bar",
  "title": "7-day calories vs target",
  "subtitle": "5 logged days",
  "unit": "kcal",
  "x": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
  "series": [
    {
      "name": "Intake",
      "values": [2150, 1980, null, 2340, null, 2050, 1840],
      "low": [1950, 1800, null, 2100, null, 1850, 1680],
      "high": [2350, 2160, null, 2580, null, 2250, 2000]
    }
  ],
  "ref": { "value": 2200, "label": "Target" },
  "caption": "Unlogged days are shown as gaps, not zeroes."
}
```
- Use `null` for missing data (draws an honest gap, never a fake zero).
- Add `"low"` and `"high"` arrays for uncertainty whiskers (`bar`/`hbar`) or shaded confidence bands (`line`/`area`).
- Add `"stacked": true` on `bar` or `area` to stack series.
- Add `"predicted": true` on a series or item for dashed model-estimate styling.
- Vertical `bar` charts with >7 categories or long labels automatically flip to horizontal bars on narrow phone screens.

### Shorthand items form (`hbar`, `bar`, `donut`, `progress`, or ```` ```compare ````)
```chart
{
  "type": "hbar",
  "title": "Macros today",
  "unit": "g",
  "items": [
    { "label": "Protein", "value": 124, "low": 112, "high": 136, "target": 140 },
    { "label": "Carbs", "value": 210, "target": 260 },
    { "label": "Fibre", "value": 18, "target": 30 }
  ]
}
```

### Scatter plot
```chart
{
  "type": "scatter",
  "title": "Latency vs quality",
  "xLabel": "First token (s)",
  "yLabel": "Score (%)",
  "series": [
    { "name": "Cloud", "points": [{ "x": 0.8, "y": 84, "label": "DeepSeek" }, { "x": 1.3, "y": 93, "label": "Neon" }] }
  ]
}
```

### Progress targets
```chart
{
  "type": "progress",
  "title": "Daily nutrition targets",
  "items": [
    { "label": "Calories", "value": 1840, "target": 2200, "unit": "kcal", "tone": "ok" },
    { "label": "Fibre", "value": 14, "target": 30, "unit": "g", "tone": "warn" }
  ]
}
```

## 2. KPI Stat Tiles (```` ```numbers ````)
Show 1 to 6 key metrics at a glance:
```numbers
[
  { "label": "Today", "value": 1840, "unit": "kcal", "delta": "-360 vs target", "tone": "ok" },
  { "label": "Protein", "value": 124, "unit": "g", "note": "89% of target", "tone": "accent" },
  { "label": "Projected", "value": 2120, "unit": "kcal", "tone": "predicted" }
]
```
Tones: `neutral`, `ok`, `warn`, `bad`, `accent`, `predicted` (dashed border).

## 3. Rich Sortable Tables (```` ```table ````)
Plain Markdown pipe tables are automatically styled with right-aligned tabular numbers and Copy TSV. Use ```` ```table ```` when you want sorting, units, inline proportion bars (`"bar": true`), search (>12 rows), or a bold summary footer:
```table
{
  "title": "Top protein sources",
  "columns": [
    "Food",
    { "key": "g", "label": "Portion", "unit": "g", "num": true },
    { "key": "p", "label": "Protein", "unit": "g", "num": true, "bar": true }
  ],
  "rows": [
    ["Chicken breast", 180, 56],
    ["Greek yogurt", 200, 20]
  ],
  "footer": ["Total", 380, 76]
}
```

## 4. Method / Plan Steps (```` ```steps ````)
```steps
{
  "title": "Release flow",
  "steps": [
    { "label": "Build", "detail": "Compile and bundle assets", "state": "done" },
    { "label": "Verify", "detail": "Unit + visual checks", "state": "active" },
    { "label": "Merge", "state": "pending" }
  ]
}
```

## 5. Callouts
Use standard Markdown alert syntax: `> [!NOTE]`, `> [!CAUTION]`, `> [!TIP]`, `> [!PREDICTION]` (with an optional custom title on the first line).

## 6. Heavy / Custom Scientific Plots
For complex multi-panel scientific figures, heatmaps, 3D renders or >300 points, save a PNG with Python (`matplotlib`/`seaborn`) and embed its absolute path in backticks on its own line (per `MEDIA_RULE`).

## Rules
- Never invent or estimate numbers not grounded in tool outputs or the user's data.
- Keep specs compact: <= 12 series, <= 300 points/series, <= 100 table rows, <= 6 stat tiles, <= 8 steps, <= 20 KB JSON.
- No HTML, scripts or URLs inside JSON specs; every string is rendered as escaped text.

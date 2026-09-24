# Journal + Heatmap — what changed

## New files

| File | Role |
|---|---|
| `journal.py` | The journal **noun**. One append-only record per fill, at `DATA_DIR/journal.jsonl`. |
| `pairing.py` | Consumer. FIFO-by-quantity pairing of fills into entry/exit legs with P/L. |
| `heatmap.py` | Consumer. The entry-grade × exit-grade grid, with layer filters. |
| `journal_import.py` | Consumer. Parses a sheet export into journal records; detects which export it is. |
| `journal_pairdetail.py` | Consumer. Rebuilds fills from a Pair Detail (one-row-per-leg) export. |
| `journal_routes.py` | Flask blueprint. All journal/heatmap endpoints. |
| `journal_sync.py` | Consumer. Alpaca fill sync + clipboard paste parsing. |
| `setup_routes.py` | Flask blueprint factory. Credentials, account type, sync, paste. |

## Changed files

**`app.py`** — six edits, all additive:
- `_journal_fill()` helper, wrapped in a bare `except` so a broken journal can never stop a trade or stall the bar loop.
- `_cond_from_ctx()` maps the engine's `step2` context onto the same condition keys the imported sheet uses, so engine and manual fills filter through one picker.
- `_do_entry()` and `_do_exit()` take an optional `conditions` argument and mirror each filled leg into the journal. Execution logic untouched.
- `_evaluate_ticker()` passes the engine's cloud read into both.
- Blueprint registered (2 lines).

**`alpaca_manager.py`** — stores the credentials on connect (the REST fallback needs them) and adds `get_fill_activities()`. It tries the SDK's activities call first and falls back to `GET /v2/account/activities/FILL`, because alpaca-py has moved that method across versions while the REST route has not. Both paths return the same normalized shape. No new dependency; the fallback uses `urllib`.

**`static/index.html`** — Setup, Journal and Performance tabs, replacing the two "next build pass" placeholders. Re-run `patch_frontend.py` safely; it detects its own marker and refuses to double-apply.

`trade_log.csv`, `signal_log.csv` and `order_log.csv` are **unchanged and still written**. The journal is additive. Delete `journal.jsonl` and the trading side keeps working.

## Why JSONL and not CSV

`trade_log.csv` has a fixed header. Grades, tag lists, free-text notes and an open-ended set of condition layers don't fit a fixed header without a schema migration per new field or a wall of empty columns. JSONL keeps the append-only, crash-tolerant, greppable properties of CSV while letting each record carry only the fields it has.

## Getting started

1. Launch as usual. Three new tabs appear.
2. **Setup**: enter your API key and secret, pick paper or live, save, then **Connect**. Blank credential fields keep whatever is already stored, so you can change account type without retyping keys.
3. **Journal**: three sources, top-left of the left panel.
   - **Alpaca** — pick a window, **Preview**, then **Sync**. Pulls fills off the connected account.
   - **Paste** — paste rows from a broker page or sheet. Needs a header line naming symbol, side, qty, price and time; TSV, CSV and markdown tables all work, and the column names are matched against a list of aliases.
   - **File** — path to an exported sheet, the original CSV importer.
4. **Performance**: the grid. Metric selector top-left, layers down the left side, click any cell to list its trades underneath.

Every source lands fills **ungraded**. Grading is always manual.

## The paper → live switch

Three guards, all deliberate, because the bar loop can place orders without asking:

1. Going live requires typing `LIVE` to confirm. A stray click cannot do it.
2. **Any** change to credentials or account type forces the engine to OBSERVE ONLY. You re-arm by hand afterwards.
3. The connection is dropped, so you reconnect and see which account the banner names before anything can trade.

Going back to paper also disarms the engine. That asymmetry is intentional — the disarm is about the account changing underneath an armed engine, not about which direction it changed.

## Dedupe

Alpaca fills carry a stable activity id, stored at `meta.activity_id`; re-syncing the same window adds nothing. Partial fills of one order arrive as separate activities and stay separate, which is correct — the pairing layer already splits scale-ins into their own legs.

Pasted rows have no such id and dedupe on symbol + timestamp-to-the-second + side + qty + price. Paste the same block twice and the second is a no-op.

The Journal tab badge shows the ungraded count. New engine fills land there automatically with their cloud conditions pre-filled — only the grade and the "why" need you.

## Validation

`GET /api/journal/reconcile` proves realized P/L plus open cash flow equals the signed sum of every fill. The Performance tab calls it on every load and shows a red warning if it ever fails. Against your 365-row export:

```
365 fills -> 199 legs
raw_net    -2993.35
realized   -2484.11
open cash   -509.24
difference      0.0   ok: true
```

Grid totals match the spreadsheet analysis to the cent.

## Fixed: sync returning 422

The first build sent `after` as `datetime.utcnow().isoformat()`, which produces `2026-09-22T21:08:42.123456`. The activities endpoint accepts exactly two shapes, `YYYY-MM-DD` and `YYYY-MM-DDTHH:MM:SSZ`, and rejects anything else with 422. Fractions of a second are not accepted anywhere in that API.

Three changes:
- `AlpacaManager._rfc3339()` normalizes every timestamp before it goes on the wire, and `journal_sync` now sends a date-only `after`.
- `urllib` HTTPErrors are read for their body, so failures say `Alpaca 422 on /v2/account/activities: {"message":"invalid format for parameter after"}` instead of a bare status code.
- Two request shapes are tried: `/v2/account/activities/FILL`, then `/v2/account/activities?activity_types=FILL`. The official Go client uses the second.

## Two export formats, detected automatically

**Journal → File** takes either sheet and works out which it is:

- **Trade Log** — one row per order. The original, with second-precision timestamps and full entry/exit conditions. Prefer it if you still have it.
- **Pair Detail** — one row per entry/exit leg, as produced by the analysis workbook.

Pair Detail is not a straight read. It is legs, not fills: a single sell closing two buys appears as two rows sharing one exit, and reading those as fills would book the sell twice. Every row carries `Entry Row` and `Exit Row`, the line numbers of the fills in the original Trade Log, so the reconstruction keys each fill by its source row and merges every leg referencing it. A row that both closes a long and opens a short collapses back into the one fill it always was.

Verified against the real data: both paths give 365 fills, 199 legs, identical grid counts in all 27 populated cells, the same 63% win rate, and totals two cents apart (prices are recovered from rounded dollar amounts).

What Pair Detail loses: timestamps are minute-truncated rather than to the second, and exit-side 1H/1D conditions are not among its columns.

## Getting your graded history in

Use **Journal → File**, not Alpaca sync. The file importer reads the Grade column and the condition columns; every fill lands already graded and the Performance tab populates immediately. Alpaca has no idea what you scored a trade, so sync can only ever produce ungraded fills.

If a sync already pulled that same history as ungraded fills, **Preview removal** / **Remove ungraded synced** in the Alpaca panel clears them. It only touches records with `source: engine` and no grade, so hand-graded work is never in the blast radius. Then run the file import.

Going forward, the Alpaca panel takes a **From / To** date range instead of a preset window. From defaults to today, so a sync never reaches back into imported history by accident. To is optional and means "up to now". Shortcut buttons: Today, 7d, 30d, and **Since last fill**, which uses the newest fill already on file.

Both ends are inclusive Eastern calendar days. They are converted to timezone-aware Eastern boundaries before hitting the API, so From = today means midnight Eastern, not a UTC midnight that reaches back into the previous session's evening. An inverted range is rejected with a 400 rather than silently returning nothing.

The API also still accepts `days`, or raw `after`/`until` timestamps, for scripted use. Calling it with no window at all pulls the entire account history; the UI will not do that, but the endpoint permits it deliberately.

## Fixed: mixed timezones across sources

Alpaca reports UTC; the sheet export records Eastern wall-clock. The first build stored each as-is, so one synced fill sat four hours adrift of the imported ones. Pairing sorts by `filled_at` within a symbol, so a sell could pair against the wrong buy.

`journal.to_et()` now normalizes at the door. Everything in `journal.jsonl` is naive Eastern. A tz-aware value gets converted; a naive one is assumed Eastern, which is what both the sheet importer and a pasted broker block produce.

## Fixed: file import returning 500

The import route caught `FileNotFoundError` and let everything else fall through as a bare 500, which hid the actual cause. Three things produced one:

- **A quoted path.** Windows "Copy as path" wraps it in double quotes. That raises `OSError 22`, not `FileNotFoundError`, so it never looked like a missing file.
- **Pointing at the .xlsx** instead of the exported .csv — a binary file hitting a text decode.
- **cp1252 content.** Excel writes it often enough, and a single smart quote breaks a hard utf-8 decode.

Now: the **File** source has a real file picker. The browser reads and decodes the file and posts its contents, so paths, drive letters, quoting and code pages never reach the server. The typed-path box is still there under "Type a path instead," and it now strips quotes, expands `~` and `%VARS%`, and tries utf-8/utf-8-sig/cp1252/latin-1 in turn.

Every predictable failure returns a 400 that says what to do. A genuinely unexpected one still returns 500, but the traceback goes to the forensics log instead of only the console. Choosing the wrong CSV (Pair Detail rather than Trade Log) now says so rather than reporting "imported 0".

## Known soft spot

Import row 259 (`SPY260811P00770000` buy, Aug 7) has cells reading 2.29 and 2.93 with no way to tell which is the fill. 2.93 is used, and the record carries `meta.uncertain`. If it should be 2.29, that leg's P/L moves by $64 — edit `ROW_OVERRIDES` in `journal_import.py` and re-import.

## Not built yet

- **Grade-on-fill prompt.** The degraded-state UI (grayed signal board, undismissable banner after an unjournaled fill) is not implemented. The badge is passive; nothing forces you to clear the queue.
- **Manual fill UI.** `POST /api/journal/fill` works; there's no form for it yet. Manual trades come in through import.
- **`compare()` has no UI.** `GET /api/heatmap/compare?key=...` returns one grid per value of a split key. Useful for "does this layer change the grid's shape or just shift it," but nothing calls it yet.

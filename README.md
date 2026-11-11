# Lidl Hellas Leaflets — Open WebUI Tool

Turn the current Lidl Hellas (Greece) leaflet PDFs into a local SQLite price store, then answer narrow price questions with small, token-budgeted tables.

A single-file **Open WebUI Tool** (workspace → Tools): one `Tools` class with a pydantic `Valves` model, no server, no sidecar, no extra API surface beyond the chat tools it registers.

`Python 3.11+` · `Open WebUI 0.4.0+` · `SQLite` · `MIT`

> Personal, unofficial project. Not affiliated with, endorsed by, or supported by Lidl.

---

## What it does

The tool is deliberately split into **two layers that never mix**.

### 1. Ingestion — `refresh_leaflets()`

The only tool that writes. One run does:

1. **Discovery** — reads the Leaflet ESI overview fragment (falling back to the human leaflets page), then asks the flyer JSON API for metadata and the PDF URL of each current leaflet for the configured region/store.
2. **Download** — streams each leaflet PDF into `cache_dir`, with a polite delay between requests and a hash of the bytes for change detection.
3. **Layout extraction** — sends the PDF to [docling-serve](https://github.com/docling-project/docling-serve) for layout-aware extraction (text items with bounding boxes), falling back to `pypdf` geometry — visitor-based, with a layout-mode fallback — when docling is unavailable or returns nothing.
4. **Canonicalisation** — rebuilds lines, cells and name blocks from the geometry, associates each offer tile with its headline price, unit price, size, discount badge and promo text, folds Greek accents into a `product_key`, and assigns a category.
5. **Snapshot** — appends rows to SQLite. Nothing is ever mutated in place.

### 2. Retrieval — every other tool

Everything else is **read-only SQL** that returns one small table, capped by `max_tool_output_tokens` (default **800** tokens, ~4 chars/token). When the budget is exceeded the table is cut on a line boundary and a footer tells the model how many rows were hidden and how to narrow the query.

Leaflet page text **never enters the model context**. Only the handful of rows the question actually needs does.

Every price row carries provenance:

| Field | Meaning |
| --- | --- |
| `leaflet_id` | which leaflet the price came from |
| `page` | page number inside that PDF |
| `snapshot_date` | the leaflet's `valid_from` date — the day this observation belongs to |
| `valid_from` / `valid_to` | the offer window, joined from `leaflets` |
| `source_url` | the exact PDF URL, joined from `leaflets` |
| `confidence` | parse confidence, `0.0`–`1.0` |

Historical rows are **never updated**. If a product's price changes, a new snapshot row appears next to the old one — which is exactly what makes `get_price_history()` and `check_seasonal_item()` meaningful.

---

## Why

An LLM context window is the most expensive storage you own, and a leaflet PDF is 40–80 pages of dense layout: columns, price flashes, unit prices, "Ισχύει έως" banners. Pasting that into a chat is slow, costly, and mostly irrelevant to the one question being asked.

Store the structure once, locally, for free. Then answer `Πόσο έχει το ελαιόλαδο;` with a five-row table.

---

## Repository layout

```
openwebui-lidl-leaflets-tool/
├── lidl_leaflets_tool.py   # the whole tool: Tools class + nested Valves model
├── README.md
└── LICENSE
```

One file, no package, no build step — it is meant to be pasted into Open WebUI's Tools panel.

---

## Install (Open WebUI)

1. **Workspace → Tools → “+ New Tool”**.
2. Name it `lidl_leaflets` and paste (or import) the contents of `lidl_leaflets_tool.py`.
3. **Save**. Open WebUI reads the docstring header at the top of the file:

   ```python
   """
   title: Lidl Hellas Leaflet Intelligence (structured price store)
   version: 2.0.0
   required_open_webui_version: 0.4.0
   requirements: httpx, pypdf
   """
   ```

   `httpx` and `pypdf` are installed for you on first load; `required_open_webui_version` gates the tool to 0.4.0 and newer.
4. Open the tool's **Valves** (gear icon) and set at minimum `db_path`, `cache_dir` and — if you run docling-serve — `docling_url`.
5. In a chat, enable the tool from the tools menu, then ask a question.

> The database and schema are created on the first `refresh_leaflets()` call; the parent directories of `db_path` and `cache_dir` are created automatically. Every other tool refuses politely (rather than crashing) until the store exists.

Ingestion and retrieval tools also emit Open WebUI **status** events while they work and a **citation** event with the leaflet URL and page numbers behind each answer.

---

## Configuration valves

All endpoints are valves — nothing is hard-coded in the logic.

| Valve | Default | Purpose |
| --- | --- | --- |
| `base_url` | `https://www.lidl-hellas.gr` | Site root, used for the human leaflet page fallback and as the citation URL when a row has none. |
| `overview_url` | `https://esi.leaflets.schwarz/overview` | Leaflet ESI overview fragment — primary discovery source. |
| `overview_page` | `https://www.lidl-hellas.gr/c/fylladio-lidl/s10020481` | Human-facing leaflets page, used only if the ESI overview fails. |
| `api_base` | `https://endpoints.leaflets.schwarz/v4` | Flyer JSON API base (metadata + PDF URL). |
| `client_locale` | `lidl/el-GR` | Client/locale pair sent to the ESI and API endpoints. |
| `region_id` | `0` | Leaflet region id (`0` = national). |
| `store_id` | `0` | Leaflet store id (`0` = national). |
| `docling_url` | `http://docling-serve:5001` | docling-serve base URL. Leave empty to force `pypdf`. |
| `docling_timeout_s` | `900` | Per-PDF timeout for docling-serve (leaflet conversion is slow). |
| `db_path` | `/data/lidl/leaflets.db` | SQLite file. **Must live on a mounted volume.** |
| `cache_dir` | `/data/lidl/cache` | Downloaded PDFs. **Must live on a mounted volume.** |
| `max_pages_per_leaflet` | `80` | Hard page cap per PDF; guards against runaway documents. |
| `max_leaflets_per_refresh` | `2` | How many recent leaflets one refresh ingests. |
| `max_tool_output_tokens` | `800` | Token budget for every tool return (~4 chars/token). |
| `min_parse_confidence` | `0.5` | Rows below this are stored with `needs_review=1` and hidden from price answers. |
| `request_timeout_s` | `60` | HTTP timeout for discovery and PDF downloads. |
| `user_agent` | `OpenWebUI-LidlTool/2.0 (+personal use)` | Sent on every outbound request. |
| `enable_ocr` | `False` | Passed to docling-serve as `do_ocr`; needed for scanned, image-only pages. |
| `request_delay_s` | `1.0` | Delay between outbound Lidl requests (politeness / rate limiting). |

---

## Tools

Signatures are exactly as exposed to the model. Every return is truncated to `max_tool_output_tokens`.

### `refresh_leaflets(force=False)`

The only write path. Discovers the current leaflets, downloads the PDFs, extracts and canonicalises the layout, and appends a new snapshot.

- `force=False` — a leaflet whose PDF hash is unchanged since the last ingest is skipped as `unchanged` (nothing is re-parsed).
- `force=True` — re-download and re-extract regardless (useful after changing `min_parse_confidence`, `enable_ocr` or `max_pages_per_leaflet`).

Returns one line per leaflet — `leaflet_id | pages | new_rows | parsed | method` (method is `docling` or `pypdf`) — plus any extraction warnings.

### `list_leaflets()`

What is in the store right now: `leaflet | valid_from | valid_to | state (current/past) | pages | rows | conf | method`. Use it to check freshness before trusting an answer.

### `search_products(query, category="", limit=20, include_needs_review=False)`

Accent-insensitive, Greek-aware product search over the **current** leaflets (rows whose `valid_to` is today or later; if none, the most recent leaflet). Every whitespace-separated token of the query must match the `product_key`, optionally constrained by `category`. Returns `product | price € | unit | promo | valid | p`, cheapest first, plus the source URL.

### `get_price_history(product, weeks=26)`

Time series of the best-matching product across stored snapshots inside the look-back window: `date | leaflet | price € | promo | p`, followed by `first → last (Δ €, Δ %)` and the min/max over the observed snapshots. Up to two alternative matches are listed by name.

### `get_deals(category="", min_discount=0.0, limit=20)`

Current promotions — percentage badges, Lidl Plus lines, multi-buy and other promo text — from the leaflet(s) valid today (or the latest). Ordered by discount descending, then price ascending: `product | price € | off | promo | p`.

### `check_seasonal_item(product)`

Has this item appeared before, and when? Returns every sighting (`date | leaflet | price € | promo | p`), the appearance count and a verdict based on how many distinct months the item appeared in — the quick answer to "does Lidl ever stock this?".

### `export_current_leaflet(format="json", max_rows=500)`

Escape hatch for bulk analysis: the current leaflet's rows as a JSON array (default) or a Markdown table (`format="markdown"`), capped at `max_rows` (hard-clamped to 1000) and then by the token budget. Meant for offline analysis, not for feeding the model.

### Example prompts

```
Πόσο έχει το ελαιόλαδο;
Which coffee is on offer this week?
Has the XXL garden set appeared before?
```

---

## Data model

Four tables in one SQLite file (`journal_mode=WAL`, foreign keys on).

| Table | Grain | Columns |
| --- | --- | --- |
| `leaflets` | one row per ingested leaflet | `leaflet_id` (PK), `title`, `valid_from`, `valid_to`, `source_url`, `content_hash`, `pages`, `fetched_at`, `extraction_method` |
| `products` | one row per canonical product | `product_key` (PK), `display_name`, `brand`, `size`, `category` |
| `price_snapshots` | **append-only** observation | `snapshot_id` (PK), `leaflet_id` → `leaflets`, `product_key` → `products`, `price`, `unit_price`, `unit`, `promo_text`, `discount_pct`, `page`, `raw_text`, `snapshot_date`, `confidence`, `needs_review` |
| `ingest_log` | one row per ingest attempt | `id`, `started_at`, `finished_at`, `leaflet_id`, `status`, `rows_inserted`, `error` |

`price_snapshots` is append-only with a unique index on:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_unique
    ON price_snapshots (leaflet_id, page, product_key, snapshot_date);
```

so re-running a refresh for the same day is idempotent (`INSERT OR IGNORE`), while a new day or a new leaflet always produces new rows. No `UPDATE` of historical prices ever happens — only inserts. Supporting indexes: `idx_snap_date` and `idx_snap_leaflet`.

**Greek-aware folding.** Product keys and search both use accent-insensitive normalisation: Unicode NFD, strip combining marks, lowercase, and map final sigma `ς` → `σ`. So `Ελαιόλαδο`, `ελαιολαδο` and `ΕΛΑΙΌΛΑΔΟ` all reduce to the same key, and search works whether or not the user types accents. Non-alphanumeric characters are stripped and size tokens (e.g. `500g`) are appended to the key as `name|size`.

---

## Source endpoints

Four endpoints, all overridable via valves:

| Purpose | Endpoint |
| --- | --- |
| ESI overview fragment (discovery) | `https://esi.leaflets.schwarz/overview` |
| Flyer JSON API | `https://endpoints.leaflets.schwarz/v4/flyer?flyer_identifier=<uuid>` |
| PDF assets | `https://assets.leaflets.schwarz/leaflets/pdfs/<uuid>/<slug>.pdf` (the exact URL comes from the flyer JSON) |
| docling-serve conversion | `POST {docling_url}/v1/convert/source` |

The crawler behaves politely: `request_delay_s` (default **1.0 s**) between requests, `request_timeout_s` bounded, discovery capped at the first 8 flyer tiles, `max_leaflets_per_refresh` and `max_pages_per_leaflet` capped, and a descriptive `user_agent` on every call. If you change the caps or the delay, keep them conservative.

---

## Docker / volume note

`db_path` and `cache_dir` **must** live on a mounted volume. If they sit inside the container's writable layer, every snapshot you built disappears on the next `docker compose up --force-recreate` — and the whole point of the tool is history.

```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    ports:
      - "3000:8080"
    volumes:
      - open-webui-data:/app/backend/data
      - lidl-data:/data/lidl          # <-- db_path + cache_dir live here
    depends_on:
      - docling-serve

  # Optional: layout-aware PDF extraction. Leave docling_url empty to use pypdf.
  docling-serve:
    image: ghcr.io/docling-project/docling-serve:latest
    ports:
      - "5001:5001"

volumes:
  open-webui-data:
  lidl-data:
```

With the snippet above, keep the default valves (`db_path=/data/lidl/leaflets.db`, `cache_dir=/data/lidl/cache`) and set `docling_url=http://docling-serve:5001` (or empty it to force `pypdf`).

---

## Limitations

v1 is honest about being v1:

- **Geometry, not semantics.** Tile-to-price association is a bbox-proximity heuristic with a "closest headline price wins" rule. Multi-column layouts and price flashes that sit far from their product name can be mis-associated.
- **Derived prices are guesses.** Prices that only appear inside an `από X σε Y` / `σε Y` phrase (Lidl Plus, crossed-out) are penalised in confidence but not always separable from shelf prices.
- **Category map is a keyword heuristic.** Around 11 Greek keyword categories plus an `Άλλο` (other) fallback. Unusual products land in `Άλλο`.
- **docling-serve is optional.** The `pypdf` fallback works but is visibly worse on dense pages; confidence scores reflect that.
- **Scanned pages need OCR.** Image-only pages yield little or nothing unless `enable_ocr=True` (a docling-serve option), and OCR is slower and less accurate.
- **Low confidence is quarantined, not guessed.** Rows below `min_parse_confidence` are stored with `needs_review=1` and excluded from normal answers.
- **Renamed packagings are a known miss.** Name matching is string-based, so a rebranded product may not link to its own history.
- **Freshness is your job.** Prices are only as current as the last `refresh_leaflets()` run. The tool does not poll in the background.
- **Not affiliated with Lidl.** Personal, unofficial project. Respect Lidl's terms of use and robots policy; keep the delays and caps in place.

---

## License

MIT — see [LICENSE](https://github.com/jimchrist-x/openwebui-lidl-leaflets-tool/blob/main/LICENSE).

© 2026 Jim Christodoulou (jimchrist-x)

---

## Disclaimer

This project is for personal and educational use. It downloads publicly available Lidl Hellas leaflets at a deliberately polite rate, stores the extracted rows locally, and makes no warranty about price accuracy — always check the leaflet or the shelf price before relying on a number.

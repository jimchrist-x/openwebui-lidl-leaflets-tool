"""
title: Lidl Hellas Leaflet Intelligence (structured price store)
author: open-webui-user
author_url: https://www.lidl-hellas.gr
version: 2.0.0
required_open_webui_version: 0.4.0
requirements: httpx, pypdf
description: Discovers the current Lidl Hellas leaflets, extracts offer tiles into a local SQLite price store with snapshot history, and answers narrow price questions without loading leaflet text into the model context.
"""
# Design notes (read me)
# ----------------------
# Two layers, deliberately separated:
#   1. INGESTION  refresh_leaflets() -> discovery (ESI overview + flyer JSON API),
#      PDF download, layout extraction (docling-serve, else pypdf geometry),
#      canonicalisation, append-only SQLite snapshots.
#   2. RETRIEVAL  every other tool runs read-only SQL and returns a token-budgeted
#      table (default ~800 tokens). Leaflet page text never enters the context.
#
# Every price row carries provenance: leaflet_id, page, source_url, valid_from,
# valid_to, snapshot_date, confidence. Historical rows are never updated.
#
# Verified endpoints (Nov 2026, see Recon Report):
#   https://esi.leaflets.schwarz/overview?client_locale=lidl/el-GR&region_id=0&store_id=0
#   https://endpoints.leaflets.schwarz/v4/flyer?flyer_identifier=<uuid>
#   https://assets.leaflets.schwarz/leaflets/pdfs/<uuid>/<slug>.pdf
#   docling-serve: POST {docling_url}/v1/convert/source   (JSON, file_sources base64)
import asyncio
import base64
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
import httpx
from pydantic import BaseModel, Field
from pypdf import PdfReader
# --------------------------------------------------------------------------- regexes
MONEY = r"(?:\d{1,3}(?:\.\d{3})*,\d{2}|\d+)"
PRICE_CELL = re.compile(rf"^(?:(?P<pct>-\d{{1,2}}%)\s*)?(?P<val>{MONEY})\s*€$")
MONEY_CELL = re.compile(rf"^(?:-\d{{1,2}}%\s*)?{MONEY}\s*€$|^-\d{{1,2}}%$")
UNIT = re.compile(r"1\s*(?:Kg|Κg|KG|kg|Λ|λ|L|lt|τεμ\.?)\s*=", re.I)
UNIT_VAL = re.compile(rf"({MONEY})\s*€")
SIZE = re.compile(
    r"(\d+(?:[.,]\d+)?\s*(?:g|gr|kg|Kg|ml|lt|L)\b"
    r"|\d+\s*[x×]\s*\d+\s*(?:g|ml|τεμ\.?)"
    r"|\(\s*\d+\s*τεμ\.?"
    r"|Συσκ\.\s*[\d.,]+\s*(?:kg|g|L|lt))"
)
PCT = re.compile(r"-\d{1,2}\s*%")
PROMO = re.compile(
    r"(Με Lidl Plus|Μόνο για λίγο|1\+1|Δώρο|Έως \d+|Όφελος: [\d.,]+€"
    r"|Συσκ\. γκάμας: [\d.,]+€|Ισχύει|Lidl Plus)",
    re.I,
)
JUNK = re.compile(
    r"(Από\s+(?:Πέμπτη|Δευτέρα|Τρίτη|Τετάρτη|Παρασκευή|Σάββατο|Κυριακή)|www\.|lidl-hellas"
    r"|^Lidl$|^Με\d?\s*Plus|Plus$|Έκπτωση|Μπες στο|^Κάθε\b|Κέρδισε|^Συσκ"
    r"|Ισχύει|^σετ\b|^Μόνο με\b|^\d+\s*(?:Τεμάχια|τεμ\.?|x)\b|^Συσκ|^Όφελος)",
    re.I,
)
LETTERS = re.compile(r"[Α-Ωα-ωA-Za-z]")
SIZE_TOKEN = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:g|gr|kg|Kg|ml|lt|L)\b", re.I)
# Greek category keyword map (v1 heuristic; documented in Limitations)
CATEGORY_MAP: Dict[str, Tuple[str, ...]] = {
    "Φρούτα & Λαχανικά": (
        "μηλ",
        "πατατ",
        "ντοματ",
        "αγγουρ",
        "καρότ",
        "κρεμμυδ",
        "πιπερι",
        "μελιτζαν",
        "κολοκυθ",
        "μαρουλ",
        "σπανακ",
        "μπαναν",
        "πορτοκαλ",
        "λεμον",
        "κλημεντιν",
        "αβοκαντ",
        "σταφυλ",
        "ροδακιν",
        "λωτ",
        "βανιλ",
        "μανιταρ",
        "μπροκολ",
        "καροτ",
    ),
    "Κρέας & Πουλερικά": (
        "κοτοπουλ",
        "χοιριν",
        "μοσχαρ",
        "μπιφτεκ",
        "παϊδάκ",
        "παιδακ",
        "λουκάνικ",
        "σαλάμι",
        "μπέικον",
        "μπεικον",
        "κεμπάπ",
        "σουβλάκ",
        "ρολό",
        "πανσέτ",
        "φτερούγ",
        "μπούτι",
        "χάμπουργκ",
        "μπουργκ",
    ),
    "Ψάρια & Θαλασσινά": (
        "ψαρ",
        "σολομ",
        "γαρίδ",
        "καλαμάρ",
        "τόνο",
        "τονο",
        "μπακαλιάρ",
    ),
    "Αλλαντικά & Τυριά": (
        "τυρ",
        "κασέρ",
        "γραβιέρ",
        "φέτα",
        "φετα",
        "γαλοπουλ",
        "ανθότυρ",
    ),
    "Γαλακτοκομικά": (
        "γάλα",
        "γαλα",
        "γιαούρτ",
        "γιαουρτ",
        "κρέμα γάλακτ",
        "βούτυρ",
        "αυγ",
    ),
    "Αρτοποιία & Ζαχαροπλαστική": (
        "ψωμ",
        "αρτοσκεύασ",
        "κρουασάν",
        "κρουασαν",
        "πίτα",
        "πιτα",
        "ντόνατ",
        "ντονατ",
        "μπισκότ",
        "μπισκοτ",
        "κέικ",
        "κεικ",
        "τυρόπιτ",
        "σοκολάτ",
        "σοκολατ",
        "κριτσίν",
    ),
    "Βασικά Τρόφιμα": (
        "ελαιόλαδ",
        "ελαιολαδ",
        "λάδι",
        "λαδι",
        "ρύζ",
        "ρυζ",
        "ζυμαρικ",
        "μακαρόν",
        "πένν",
        "αλεύρ",
        "αλευρ",
        "ζάχαρ",
        "ζαχαρ",
        "αλάτι",
        "όσπρι",
        "φακ",
        "φασολ",
        "τόνο",
        "τοματ",
        "σάλτσ",
        "πελτ",
        "τουρσ",
        "νερό",
        "μελι",
    ),
    "Ποτά & Καφές": (
        "καφ",
        "χυμ",
        "cola",
        "κόλα",
        "μπύρ",
        "μπυρ",
        "νερό",
        "αναψυκτ",
        "τσάι",
    ),
    "Καθαρισμός & Σπίτι": (
        "καθαριστ",
        "απορρυπαντ",
        "χαρτί",
        "σακούλ",
        "τζάμι",
        "τζαμι",
        "λέρωσ",
        "μπλοκ",
        "wc",
        "πλυντήρ",
        "μαλακτικ",
        "σφουγγαρ",
    ),
    "Κατοικίδια": ("σκύλ", "σκυλ", "γάτ", "γατ", "ζωοτροφ"),
    "Εποχικά / Non-food": (
        "xxl",
        "ρούχ",
        "ρουχ",
        "παπούτσ",
        "παπουτσ",
        "εργαλ",
        "κήπ",
        "κηπ",
        "παιχνίδ",
        "κουζίν",
        "κουζιν",
        "ηλεκτρ",
    ),
}
# --------------------------------------------------------------------------- utilities
def _clean(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()
def _money(token: str) -> float:
    return float(token.replace(".", "").replace(",", "."))
def _fold(text: str) -> str:
    """Accent-insensitive, case-insensitive Greek fold (final sigma -> sigma)."""
    if not text:
        return ""
    norm = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in norm if unicodedata.category(ch) != "Mn")
    stripped = stripped.replace("ς", "σ").replace("Ά", "Α")
    return re.sub(r"\s+", " ", stripped).strip().lower()
def _letters_left(text: str) -> int:
    t = SIZE.sub(" ", text)
    t = re.sub(r"\b(?:g|gr|kg|ml|lt|L|Kg|τεμ|Συσκ)\b\.?", " ", t)
    return len(LETTERS.findall(t))
def _category_of(name: str) -> str:
    folded = _fold(name)
    for category, needles in CATEGORY_MAP.items():
        for needle in needles:
            if _fold(needle) in folded:
                return category
    return "Άλλο"
def _product_key(name: str, size: str) -> str:
    base = _fold(name)
    base = re.sub(r"[^α-ωa-z0-9 ]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    size_part = _fold(size or "")
    size_part = re.sub(r"[^a-zα-ω0-9.,]+", "", size_part)
    key = f"{base}|{size_part}" if size_part else base
    return key[:180]
def _unit_label(token: str) -> str:
    t = (token or "").strip().lower()
    if t in ("kg", "κg"):
        return "Kg"
    if t in ("l", "lt", "λ"):
        return "L"
    return "τεμ."
def _iso(value: Optional[str]) -> str:
    return (value or "")[:10]
class Tools:
    class Valves(BaseModel):
        # --- source (no hardcoded scraped URLs inside logic; every endpoint is a Valve)
        base_url: str = Field(
            default="https://www.lidl-hellas.gr",
            description="Lidl Hellas site root (used for the human leaflet page fallback).",
        )
        overview_url: str = Field(
            default="https://esi.leaflets.schwarz/overview",
            description="ESI fragment that renders the leaflet overview (primary discovery source).",
        )
        overview_page: str = Field(
            default="https://www.lidl-hellas.gr/c/fylladio-lidl/s10020481",
            description="Human leaflet page, used only if the ESI overview fails.",
        )
        api_base: str = Field(
            default="https://endpoints.leaflets.schwarz/v4",
            description="Leaflet JSON API base (metadata + PDF URL).",
        )
        client_locale: str = Field(
            default="lidl/el-GR", description="Locale sent to the ESI/API endpoints."
        )
        region_id: str = Field(
            default="0", description="Leaflet region id (0 = national)."
        )
        store_id: str = Field(
            default="0", description="Leaflet store id (0 = national)."
        )
        # --- extraction
        docling_url: str = Field(
            default="http://docling-serve:5001",
            description="docling-serve base URL. Confirmed API path: /v1/convert/source. Leave empty to force pypdf.",
        )
        docling_timeout_s: int = Field(
            default=900,
            description="docling-serve request timeout (PDF conversion is slow).",
        )
        db_path: str = Field(
            default="/data/lidl/leaflets.db",
            description="SQLite store path. MUST live on a mounted volume or history dies with the container.",
        )
        cache_dir: str = Field(
            default="/data/lidl/cache",
            description="Downloaded PDF cache directory (mounted volume).",
        )
        max_pages_per_leaflet: int = Field(
            default=80, description="Hard cap on pages parsed per leaflet."
        )
        max_leaflets_per_refresh: int = Field(
            default=2, description="How many recent leaflets one refresh ingests."
        )
        max_tool_output_tokens: int = Field(
            default=800,
            description="Hard token budget for every tool return (~4 chars/token).",
        )
        min_parse_confidence: float = Field(
            default=0.5,
            description="Rows below this confidence are needs_review and hidden from price answers.",
        )
        request_timeout_s: int = Field(
            default=60, description="HTTP timeout for discovery/PDF download."
        )
        user_agent: str = Field(
            default="OpenWebUI-LidlTool/2.0 (+personal use)",
            description="User-Agent sent to Lidl endpoints.",
        )
        enable_ocr: bool = False
        request_delay_s: float = Field(
            default=1.0,
            description="Soft 1 req/s politeness delay between Lidl requests.",
        )
    def __init__(self) -> None:
        self.valves = self.Valves()
    # ------------------------------------------------------------------ plumbing
    def _budget_chars(self) -> int:
        return max(400, int(self.valves.max_tool_output_tokens) * 4)
    async def _emit(self, emitter: Optional[Callable], kind: str, data: Any) -> None:
        if not emitter:
            return
        try:
            await emitter({"type": kind, "data": data})
        except Exception:
            pass
    async def _status(
        self, emitter: Optional[Callable], message: str, done: bool = False
    ) -> None:
        await self._emit(emitter, "status", {"description": message, "done": done})
    async def _cite(
        self, emitter: Optional[Callable], title: str, rows: List[Dict[str, Any]]
    ) -> None:
        docs = sorted(
            {r.get("source") or r.get("leaflet_id") or "leaflet" for r in rows}
        )
        urls = sorted({r.get("source_url", "") for r in rows if r.get("source_url")})
        pages = sorted({int(r.get("page", 0)) for r in rows if r.get("page")})
        payload = {
            "document": docs[:5],
            "metadata": [
                {
                    "source": ", ".join(docs[:5]) or title,
                    "url": urls[0] if urls else self.valves.base_url,
                    "page": ", ".join(str(p) for p in pages[:10]),
                    "date_accessed": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
            ],
            "source": {"name": title, "url": urls[0] if urls else self.valves.base_url},
        }
        await self._emit(emitter, "citation", payload)
    def _truncate_to_budget(
        self, text: str, shown: int, total: int, hint: str = "narrow the query"
    ) -> str:
        """Enforce the token budget on every return path, with a visible footer."""
        budget = self._budget_chars()
        if len(text) <= budget:
            if total > shown:
                return text + f"\n… {total - shown} more rows ({hint})"
            return text
        cut = text[: budget - 120]
        if "\n" in cut:
            cut = cut[: cut.rfind("\n")]
        hidden = max(total - shown, 0)
        return (
            cut
            + f"\n… output truncated to ~{self.valves.max_tool_output_tokens} tokens; {hidden} more rows ({hint})"
        )
    def _format_table(
        self, headers: List[str], rows: List[List[str]], note: str = ""
    ) -> str:
        if not rows:
            return note or "(no rows)"
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)))
        def line(cells: List[str]) -> str:
            return " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))
        out = [line(headers), "-+-".join("-" * w for w in widths)]
        out.extend(line(r) for r in rows)
        return "\n".join(out)
    # ------------------------------------------------------------------ storage
    def _db(self) -> sqlite3.Connection:
        path = self.valves.db_path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS leaflets (
              leaflet_id TEXT PRIMARY KEY,
              title TEXT, valid_from TEXT, valid_to TEXT,
              source_url TEXT, content_hash TEXT,
              pages INTEGER, fetched_at TEXT, extraction_method TEXT
            );
            CREATE TABLE IF NOT EXISTS products (
              product_key TEXT PRIMARY KEY,
              display_name TEXT, brand TEXT, size TEXT, category TEXT
            );
            CREATE TABLE IF NOT EXISTS price_snapshots (
              snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
              leaflet_id TEXT REFERENCES leaflets(leaflet_id),
              product_key TEXT REFERENCES products(product_key),
              price REAL, unit_price REAL, unit TEXT,
              promo_text TEXT, discount_pct REAL,
              page INTEGER, raw_text TEXT,
              snapshot_date TEXT NOT NULL, confidence REAL, needs_review INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ingest_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT, finished_at TEXT, leaflet_id TEXT,
              status TEXT, rows_inserted INTEGER, error TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_unique
              ON price_snapshots(leaflet_id, page, product_key, snapshot_date);
            CREATE INDEX IF NOT EXISTS idx_snap_date ON price_snapshots(snapshot_date);
            CREATE INDEX IF NOT EXISTS idx_snap_leaflet ON price_snapshots(leaflet_id);
            """)
        conn.commit()
    def _log_ingest(
        self,
        conn: sqlite3.Connection,
        started: str,
        leaflet_id: str,
        status: str,
        rows: int,
        error: str,
    ) -> None:
        conn.execute(
            "INSERT INTO ingest_log (started_at, finished_at, leaflet_id, status, rows_inserted, error)"
            " VALUES (?,?,?,?,?,?)",
            (
                started,
                datetime.now().isoformat(timespec="seconds"),
                leaflet_id,
                status,
                rows,
                error,
            ),
        )
        conn.commit()
    # ------------------------------------------------------------------ discovery
    async def _discover_leaflets(
        self, client: httpx.AsyncClient
    ) -> List[Dict[str, Any]]:
        """Resolve current leaflet identifiers at runtime (no hardcoded leaflet URLs)."""
        params = {
            "client_locale": self.valves.client_locale,
            "region_id": self.valves.region_id,
            "store_id": self.valves.store_id,
        }
        html = ""
        errors: List[str] = []
        for label, url, kw in (
            ("ESI overview", self.valves.overview_url, {"params": params}),
            ("leaflet page", self.valves.overview_page, {}),
        ):
            try:
                resp = await client.get(
                    url, timeout=self.valves.request_timeout_s, **kw
                )
                resp.raise_for_status()
                html = resp.text
                if "data-track-id" in html:
                    break
                errors.append(f"{label}: no leaflet tiles")
            except Exception as exc:
                errors.append(f"{label}: {exc}")
        if not html:
            raise RuntimeError("leaflet discovery failed: " + " | ".join(errors))
        uuids = re.findall(r'data-track-id="([0-9a-fA-F-]{36})"', html)
        names = re.findall(r'data-track-name="([^"]+)"', html)
        import html as html_mod
        names = [html_mod.unescape(_clean(n)) for n in names]
        tiles: List[Dict[str, Any]] = []
        seen = set()
        for idx, uuid in enumerate(uuids):
            if uuid in seen:
                continue
            seen.add(uuid)
            tiles.append({"id": uuid, "label": names[idx] if idx < len(names) else ""})
        if not tiles:
            raise RuntimeError(
                "leaflet discovery failed: no flyer UUIDs in overview HTML"
            )
        out: List[Dict[str, Any]] = []
        for tile in tiles[:8]:  # bounded: never crawl the whole list
            await asyncio.sleep(self.valves.request_delay_s)
            try:
                resp = await client.get(
                    f"{self.valves.api_base}/flyer",
                    params={
                        "flyer_identifier": tile["id"],
                        "region_id": self.valves.region_id,
                        "store_id": self.valves.store_id,
                    },
                    timeout=self.valves.request_timeout_s,
                )
                resp.raise_for_status()
                meta = (resp.json() or {}).get("flyer") or {}
            except Exception as exc:
                errors.append(f"meta {tile['id'][:8]}: {exc}")
                continue
            pages = meta.get("pages") or []
            pdf = meta.get("pdfUrl") or meta.get("hiResPdfUrl") or ""
            if not pdf or not pages:
                continue
            out.append(
                {
                    "id": tile["id"],
                    "label": tile.get("label") or meta.get("name") or "",
                    "title": _clean(meta.get("title")),
                    "valid_from": _iso(meta.get("startDate")),
                    "valid_to": _iso(meta.get("endDate")),
                    "page_count": len(pages),
                    "pdf_url": pdf,
                    "download_allowed": bool(meta.get("enableDownload", True)),
                }
            )
        if not out:
            raise RuntimeError(
                "leaflet discovery failed: no downloadable flyers. "
                + " | ".join(errors)
            )
        out.sort(key=lambda f: (f["valid_from"], f["valid_to"]), reverse=True)
        return out
    async def _download(
        self, client: httpx.AsyncClient, leaflet: Dict[str, Any], force: bool
    ) -> Dict[str, Any]:
        os.makedirs(self.valves.cache_dir, exist_ok=True)
        safe = (
            re.sub(r"[^A-Za-z0-9._-]+", "_", leaflet["pdf_url"].rsplit("/", 1)[-1])[:80]
            or "leaflet.pdf"
        )
        target = os.path.join(self.valves.cache_dir, safe)
        if not force and os.path.exists(target) and os.path.getsize(target) > 1024:
            with open(target, "rb") as fh:
                if fh.read(5) == b"%PDF-":
                    return {
                        "path": target,
                        "bytes": os.path.getsize(target),
                        "cached": True,
                    }
        await asyncio.sleep(self.valves.request_delay_s)
        async with client.stream(
            "GET", leaflet["pdf_url"], timeout=self.valves.request_timeout_s
        ) as resp:
            resp.raise_for_status()
            with open(target + ".part", "wb") as fh:
                async for chunk in resp.aiter_bytes(65536):
                    fh.write(chunk)
        os.replace(target + ".part", target)
        return {"path": target, "bytes": os.path.getsize(target), "cached": False}
    # ------------------------------------------------------------------ extraction
    async def _extract_docling(self, client, pdf_path):
        if not self.valves.docling_url:
            return [], "", "docling disabled"
        with open(pdf_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        filename = os.path.basename(pdf_path)
        options = {
            "from_formats": ["pdf"],
            "to_formats": ["md", "json"],
            "do_ocr": bool(self.valves.enable_ocr),
            "table_mode": "fast",
            "image_export_mode": "placeholder",
        }
        src = {"base64_string": b64, "filename": filename}
        variants = {
            "sources": {"options": options, "sources": [{"kind": "file", **src}]},
            "file_sources": {"options": options, "file_sources": [src]},
        }
        order = getattr(self, "_docling_schema_order", ["sources", "file_sources"])
        url = self.valves.docling_url.rstrip("/") + "/v1/convert/source"
        errors = []
        for name in order:
            resp = await client.post(
                url, json=variants[name], timeout=self.valves.docling_timeout_s
            )
            if resp.status_code == 422:
                errors.append(f"{name}: {resp.text[:300]}")
                continue
            if resp.is_error:
                errors.append(f"{name}: HTTP {resp.status_code} {resp.text[:300]}")
                continue
            self._docling_schema_order = [name] + [n for n in order if n != name]
            body = resp.json()
            doc = body.get("document") or (body.get("documents") or [{}])[0]
            items = self._items_from_docling(
                doc.get("json_content") or {}, doc.get("md_content") or ""
            )
            if not items:
                return (
                    [],
                    "",
                    f"docling returned no text items (status={body.get('status')})",
                )
            return items, "docling", ""
        raise RuntimeError("docling rejected both schemas → " + " | ".join(errors))
    def _items_from_docling(self, doc_json: Any, md: str) -> List[Dict[str, Any]]:
        """Walk a DoclingDocument JSON for text items with bbox (font size ~ bbox height)."""
        items: List[Dict[str, Any]] = []
        seen = set()
        def walk(node: Any, page_hint: Optional[int] = None) -> None:
            if isinstance(node, dict):
                page_no = node.get("page_no") or page_hint
                bbox = None
                prov = node.get("prov")
                if isinstance(prov, list) and prov:
                    first = prov[0] or {}
                    bbox = first.get("bbox") or None
                    page_no = first.get("page_no") or page_no
                text = node.get("text") or node.get("orig") or ""
                if bbox and isinstance(text, str) and text.strip():
                    top, bottom = bbox.get("t"), bbox.get("b")
                    left, right = bbox.get("l"), bbox.get("r")
                    if None not in (top, bottom, left, right):
                        origin = str(bbox.get("coord_origin", "TOPLEFT")).upper()
                        y = -(float(top) if origin.startswith("TOP") else float(bottom))
                        height = abs(float(top) - float(bottom)) or 8.0
                        key = (
                            int(page_no or 0),
                            round(float(left), 1),
                            round(float(top), 1),
                            text[:40],
                        )
                        if key not in seen:
                            seen.add(key)
                            items.append(
                                {
                                    "page": int(page_no or 0),
                                    "text": _clean(text),
                                    "x": float(left),
                                    "y": y,
                                    "w": max(abs(float(right) - float(left)), 4.0),
                                    "size": round(max(height, 4.0), 1),
                                }
                            )
                for value in node.values():
                    walk(value, page_no)
            elif isinstance(node, list):
                for value in node:
                    walk(value, page_hint)
        if doc_json:
            walk(doc_json)
        if items:
            return items
        # markdown fallback: synthesise geometry so the same tile logic can run
        for row, line in enumerate((md or "").splitlines()):
            if not line.strip():
                continue
            pos = 0
            for part in re.split(r"( {2,}|\|)", line):
                if part and part.strip() and part.strip() != "|":
                    t = _clean(part)
                    items.append(
                        {
                            "page": 0,
                            "text": t,
                            "x": float(pos),
                            "y": float(-row * 12),
                            "w": max(len(t) * 5.2, 4.0),
                            "size": 10.0,
                        }
                    )
                pos += len(part)
        return items
    def _items_pypdf(self, page: Any) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        def visitor(
            text: str, cm: Any, tm: Any, font_dict: Any, font_size: Any
        ) -> None:
            t = _clean(text)
            if not t:
                return
            try:
                fs = float(font_size or 0)
            except Exception:
                fs = 0.0
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            factor = 0.36 if fs >= 20 else 0.52  # condensed headline font vs. body font
            items.append(
                {
                    "text": t,
                    "x": round(x, 1),
                    "y": round(y, 1),
                    "size": round(fs, 1),
                    "w": round(max(len(t) * max(fs, 4) * factor, 4.0), 1),
                }
            )
        try:
            page.extract_text(visitor_text=visitor)
        except Exception:
            items = []
        if len(items) < 5:  # scanned / exotic page
            layout = ""
            try:
                layout = page.extract_text(extraction_mode="layout") or ""
            except Exception:
                layout = ""
            for row, line in enumerate(layout.splitlines()):
                if not line.strip():
                    continue
                pos = 0
                for part in re.split(r"( {2,})", line):
                    if part.strip():
                        t = _clean(part)
                        items.append(
                            {
                                "text": t,
                                "x": float(pos),
                                "y": float(1000 - row * 12),
                                "w": max(len(t) * 5.2, 4.0),
                                "size": 10.0,
                            }
                        )
                    pos += len(part)
        return items
    def _build_cells(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        items = sorted(items, key=lambda i: (-i["y"], i["x"]))
        lines: List[Dict[str, Any]] = []
        for it in items:
            if lines and abs(lines[-1]["y"] - it["y"]) <= 5:
                lines[-1]["items"].append(it)
            else:
                lines.append({"y": it["y"], "items": [it]})
        cells: List[Dict[str, Any]] = []
        for ln in lines:
            ln["items"].sort(key=lambda i: i["x"])
            cur = None
            for it in ln["items"]:
                if (
                    cur
                    and it["x"] - (cur["x"] + cur["w"]) <= 30
                    and not MONEY_CELL.match(cur["text"])
                    and not MONEY_CELL.match(it["text"])
                ):
                    cur["text"] += " " + it["text"]
                    cur["w"] = max(cur["w"], it["x"] + it["w"] - cur["x"])
                    cur["size"] = max(cur["size"], it["size"])
                else:
                    cur = dict(it)
                    cur["y"] = ln["y"]
                    cells.append(cur)
        return cells
    def _name_blocks(self, cells: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        def is_name(c: Dict[str, Any]) -> bool:
            t = c["text"]
            if "€" in t or PRICE_CELL.match(t) or UNIT.search(t) or PCT.search(t):
                return False
            if JUNK.search(t) or PROMO.search(t):
                return False
            if _letters_left(t) < 3 or re.match(r"^[\d.,]+\s", t):
                return False
            return 7 <= c["size"] <= 18  # >=20 is a price or a marketing headline
        names = sorted(
            [c for c in cells if is_name(c)], key=lambda c: (-c["y"], c["x"])
        )
        blocks: List[Dict[str, Any]] = []
        for c in names:
            for b in blocks:
                last_y = b["cells"][-1]["y"]
                bx0 = min(x["x"] for x in b["cells"])
                if (
                    (last_y - c["y"]) <= 18
                    and abs(c["x"] - bx0) <= 30
                    and len(b["cells"]) < 6
                ):
                    b["cells"].append(c)
                    break
            else:
                blocks.append({"cells": [c]})
        for b in blocks:
            cs = sorted(b["cells"], key=lambda c: (-c["y"], c["x"]))
            b["name"] = _clean(" ".join(c["text"] for c in cs))
            b["x0"] = min(c["x"] for c in cs)
            b["x1"] = max(c["x"] + c["w"] for c in cs)
            b["y_top"] = max(c["y"] for c in cs)
            b["y_bot"] = min(c["y"] for c in cs)
        return blocks
    @staticmethod
    def _hgap(cell: Dict[str, Any], block: Dict[str, Any]) -> float:
        if cell["x"] <= block["x1"] and cell["x"] + cell["w"] >= block["x0"]:
            return 0.0
        return min(
            abs(cell["x"] - block["x1"]), abs(block["x0"] - (cell["x"] + cell["w"]))
        )
    def _is_unit_price(self, cell: Dict[str, Any], cells: List[Dict[str, Any]]) -> bool:
        """True for prices that belong to a '1 Kg = από X σε Y' expression or to the
        small 'σε Y' continuation line (Lidl-Plus / crossed-out prices)."""
        for o in cells:
            if o is cell:
                continue
            dy = abs(o["y"] - cell["y"])
            if dy <= 5 and o["x"] < cell["x"] and cell["x"] - o["x"] <= 60:
                if UNIT.search(o["text"]) or re.search(
                    r"σε\s*$|/\s*$|=\s*$|Από\s*$", o["text"]
                ):
                    return True
            if 0 < o["y"] - cell["y"] <= 16 and o["size"] < 14:
                if abs(o["x"] - cell["x"]) <= 45 and re.search(
                    r"(^|\s)σε\s*$|από\s*$|/\s*$", o["text"], re.I
                ):
                    return True
        return False
    def _normalize_records(
        self, items: List[Dict[str, Any]], page_no: int
    ) -> List[Dict[str, Any]]:
        """Turn positioned text items of one page into canonical offer records."""
        cells = self._build_cells(items)
        blocks = self._name_blocks(cells)
        if not blocks:
            return []
        prices = [
            c
            for c in cells
            if PRICE_CELL.match(c["text"]) and not self._is_unit_price(c, cells)
        ]
        prices.sort(key=lambda p: (-p["size"], -p["y"]))
        # prices that also occur inside a "από X σε Y" / "σε Y" phrase are derived
        # (Lidl-Plus or crossed-out) prices, not shelf prices
        derived: set = set()
        for c in cells:
            if re.search(r"(^|\s)(σε|Σε)\s|\bαπό\b", c["text"], re.I):
                for tok in re.findall(
                    rf"({MONEY})\s*€",
                    c["text"]
                    + " "
                    + "".join(
                        o["text"]
                        for o in cells
                        if abs(o["y"] - c["y"]) <= 1 and 0 < o["x"] - c["x"] <= 45
                    ),
                ):
                    try:
                        derived.add(round(_money(tok), 2))
                    except ValueError:
                        pass
        headline: Dict[int, Dict[str, Any]] = {}
        head_dy: Dict[int, float] = {}
        others: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(len(blocks))}
        for p in prices:
            scored: List[Tuple[float, int]] = []
            for i, b in enumerate(blocks):
                if p["y"] > b["y_top"]:
                    dy = p["y"] - b["y_top"]
                else:
                    dy = max(0.0, b["y_bot"] - p["y"])
                hg = self._hgap(p, b)
                if dy > 130 or hg > 230:
                    continue
                scored.append((dy * 1.5 + hg, i))
            if not scored:
                continue
            scored.sort()
            target = next((i for _, i in scored if i not in headline), None)
            if target is None:
                others[scored[0][1]].append(p)
            else:
                headline[target] = p
                head_dy[target] = next((d for d, i in scored if i == target), 0.0)
        records: List[Dict[str, Any]] = []
        for i, b in enumerate(blocks):
            head = headline.get(i)
            if not head:
                continue
            win = [
                c
                for c in cells
                if c is not head
                and b["y_bot"] - 130 <= c["y"] <= b["y_top"] + 25
                and self._hgap(c, b) <= 85
            ]
            win.sort(key=lambda c: (-c["y"], c["x"]))
            size = ""
            for c in [head] + win:
                m = SIZE.search(c["text"])
                if m and "=" not in c["text"]:
                    size = m.group(1)
                    break
            if not size:
                m = SIZE.search(b["name"])
                if m:
                    size = m.group(1)
            unit_price, unit_text = None, ""
            for c in win:
                if UNIT.search(c["text"]) and self._hgap(c, b) <= 60:
                    m = re.search(r"1\s*(Kg|Κg|KG|kg|Λ|λ|L|lt|τεμ\.?)", c["text"], re.I)
                    if m:
                        unit_text = _unit_label(m.group(1))
                    val = UNIT_VAL.search(c["text"])
                    if val and unit_price is None:
                        unit_price = _money(val.group(1))
                    break
            pct = ""
            for c in win:
                m = PCT.search(c["text"])
                if m and self._hgap(c, b) <= 90:
                    pct = m.group(0)
                    break
            promos = [
                c["text"]
                for c in win
                if PROMO.search(c["text"])
                and len(c["text"]) < 60
                and self._hgap(c, b) <= 60
            ]
            rec_price = _money(PRICE_CELL.match(head["text"]).group("val"))
            joined = " ".join([b["name"]] + [c["text"] for c in win])
            lp = bool(re.search(r"Lidl\s*Plus", joined, re.I))
            conf = 0.35
            if head["size"] >= 14:
                conf += 0.20
            if size:
                conf += 0.15
            if unit_price is not None:
                conf += 0.15
            if pct or promos:
                conf += 0.05
            if len(b["cells"]) >= 2:
                conf += 0.05
            if len(others.get(i, [])) > 2:
                conf -= 0.15
            if head_dy.get(i, 0.0) > 60:
                conf -= 0.10
            if round(rec_price, 2) in derived:
                conf -= 0.20  # looks like a derived / crossed-out price
            records.append(
                {
                    "page": page_no,
                    "name": b["name"],
                    "price": _money(PRICE_CELL.match(head["text"]).group("val")),
                    "unit_price": unit_price,
                    "unit": unit_text,
                    "size": size,
                    "category": _category_of(b["name"]),
                    "promo_text": " | ".join(dict.fromkeys(promos))[:180],
                    "discount_pct": (-float(pct.strip("-%")) if pct else None),
                    "lidl_plus": lp,
                    "confidence": round(max(0.0, min(conf, 0.95)), 2),
                    "raw_text": _clean(joined)[:240],
                    "other_prices": [
                        round(_money(PRICE_CELL.match(o["text"]).group("val")), 2)
                        for o in others.get(i, [])
                    ][:5],
                }
            )
        return records
    def _extract_pypdf(self, pdf_path: str) -> Tuple[List[Dict[str, Any]], int, str]:
        reader = PdfReader(pdf_path)
        total = len(reader.pages)
        limit = min(total, int(self.valves.max_pages_per_leaflet))
        records: List[Dict[str, Any]] = []
        failed = 0
        for idx in range(limit):
            try:
                items = self._items_pypdf(reader.pages[idx])
                records.extend(self._normalize_records(items, idx + 1))
            except Exception:
                failed += 1
                continue
        warn = f"{failed} page(s) failed extraction" if failed else ""
        if total > limit:
            warn = (warn + "; " if warn else "") + f"page cap {limit}/{total}"
        return records, total, warn
    # ------------------------------------------------------------------ ingest
    async def refresh_leaflets(
        self, force: bool = False, __event_emitter__: Optional[Callable] = None
    ) -> str:
        """Discover, download, extract and persist the current Lidl Hellas leaflet(s).
        :param force: re-download and re-extract even if the cached PDF/hash is unchanged.
        :return: compact ingest summary (leaflets, pages, rows inserted, confidence, warnings); ~<=800 tokens.
        """
        started = datetime.now().isoformat(timespec="seconds")
        conn = None
        try:
            await self._status(__event_emitter__, "Discovering current leaflets…")
            async with httpx.AsyncClient(
                headers={
                    "User-Agent": self.valves.user_agent,
                    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
                },
                follow_redirects=True,
            ) as client:
                leaflets = await self._discover_leaflets(client)
                today = date.today().isoformat()
                fresh = [
                    l
                    for l in leaflets
                    if l["valid_to"] >= (date.today() - timedelta(days=10)).isoformat()
                ]
                chosen = (fresh or leaflets)[
                    : max(1, int(self.valves.max_leaflets_per_refresh))
                ]
                conn = self._db()
                self._init_schema(conn)
                summary_rows: List[List[str]] = []
                total_inserted = 0
                notes: List[str] = []
                for n, leaflet in enumerate(chosen, start=1):
                    leaflet_id = f"lidl-{leaflet['valid_from']}-{leaflet['id'][:8]}"
                    await self._status(
                        __event_emitter__,
                        f"Downloading leaflet {n}/{len(chosen)}: {leaflet['label'] or leaflet['title']}",
                    )
                    dl = await self._download(client, leaflet, force)
                    with open(dl["path"], "rb") as fh:
                        content_hash = hashlib.sha256(fh.read()).hexdigest()[:32]
                    row = conn.execute(
                        "SELECT content_hash FROM leaflets WHERE leaflet_id=?",
                        (leaflet_id,),
                    ).fetchone()
                    if row and row["content_hash"] == content_hash and not force:
                        summary_rows.append(
                            [
                                leaflet_id,
                                str(leaflet["page_count"]),
                                "0",
                                "unchanged",
                                "-",
                            ]
                        )
                        continue
                    method, warning = "pypdf", ""
                    records: List[Dict[str, Any]] = []
                    if self.valves.docling_url:
                        await self._status(
                            __event_emitter__,
                            f"Extracting leaflet {n}/{len(chosen)} with docling-serve…",
                        )
                        try:
                            items, method, warning = await self._extract_docling(
                                client, dl["path"]
                            )
                            if items:
                                by_page: Dict[int, List[Dict[str, Any]]] = {}
                                for it in items:
                                    by_page.setdefault(
                                        int(it.get("page") or 1), []
                                    ).append(it)
                                for page_no in sorted(by_page):
                                    records.extend(
                                        self._normalize_records(
                                            by_page[page_no], page_no
                                        )
                                    )
                        except Exception as exc:
                            warning = f"docling unavailable ({type(exc).__name__}: {exc}); fell back to pypdf"
                            method, records = "pypdf", []
                    if not records:
                        if not warning:
                            warning = "docling returned no offer rows"
                        await self._status(
                            __event_emitter__,
                            f"Extracting leaflet {n}/{len(chosen)} with pypdf (fallback)",
                        )
                        records, pages_total, warn2 = await asyncio.to_thread(
                            self._extract_pypdf, dl["path"]
                        )
                        method = "pypdf"
                        warning = "; ".join(x for x in (warning, warn2) if x)
                    else:
                        pages_total = leaflet["page_count"]
                    inserted = self._persist(
                        conn, leaflet, leaflet_id, content_hash, records, method
                    )
                    total_inserted += inserted
                    conf = [r["confidence"] for r in records]
                    avg = round(sum(conf) / len(conf), 2) if conf else 0.0
                    low = sum(1 for c in conf if c < self.valves.min_parse_confidence)
                    summary_rows.append(
                        [
                            leaflet_id,
                            str(pages_total),
                            str(inserted),
                            f"{len(records)} rows, avg conf {avg}, {low} low",
                            method,
                        ]
                    )
                    if warning:
                        notes.append(f"{leaflet_id}: {warning}")
                    self._log_ingest(
                        conn,
                        started,
                        leaflet_id,
                        "ok" if records else "partial",
                        inserted,
                        warning or "",
                    )
                    await self._status(
                        __event_emitter__,
                        f"Leaflet {n}/{len(chosen)} done ({inserted} new rows)",
                    )
                await self._status(__event_emitter__, "Ingest finished", done=True)
                table = self._format_table(
                    ["leaflet_id", "pages", "new_rows", "parsed", "method"],
                    summary_rows,
                )
                head = (
                    f"Ingest complete: {len(chosen)} leaflet(s), {total_inserted} new snapshot row(s), "
                    f"db={self.valves.db_path}"
                )
                payload = head + "\n" + table
                if notes:
                    payload += "\n\nWarnings:\n- " + "\n- ".join(notes[:4])
                return self._truncate_to_budget(
                    payload, len(summary_rows), len(summary_rows)
                )
        except Exception as exc:
            await self._status(__event_emitter__, f"Ingest failed: {exc}", done=True)
            return self._truncate_to_budget(
                f"Ingest failed: {type(exc).__name__}: {exc}\n"
                f"Check Valves: overview_url / api_base reachable, db_path and cache_dir on a mounted volume "
                f"(current: db_path={self.valves.db_path}, cache_dir={self.valves.cache_dir}).",
                0,
                0,
            )
        finally:
            if conn is not None:
                conn.close()
    def _persist(
        self,
        conn: sqlite3.Connection,
        leaflet: Dict[str, Any],
        leaflet_id: str,
        content_hash: str,
        records: List[Dict[str, Any]],
        method: str,
    ) -> int:
        """Append-only persistence: price changes create new rows, existing rows are never updated."""
        conn.execute(
            "INSERT INTO leaflets (leaflet_id, title, valid_from, valid_to, source_url, content_hash,"
            " pages, fetched_at, extraction_method) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(leaflet_id) DO UPDATE SET content_hash=excluded.content_hash,"
            " fetched_at=excluded.fetched_at, extraction_method=excluded.extraction_method",
            (
                leaflet_id,
                leaflet.get("title") or leaflet.get("label") or "",
                leaflet["valid_from"],
                leaflet["valid_to"],
                leaflet["pdf_url"],
                content_hash,
                leaflet["page_count"],
                datetime.now().isoformat(timespec="seconds"),
                method,
            ),
        )
        snapshot_date = leaflet["valid_from"] or date.today().isoformat()
        inserted = 0
        for rec in records:
            key = _product_key(rec["name"], rec.get("size", ""))
            if not key:
                continue
            conn.execute(
                "INSERT INTO products (product_key, display_name, brand, size, category)"
                " VALUES (?,?,?,?,?) ON CONFLICT(product_key) DO NOTHING",
                (
                    key,
                    rec["name"],
                    (rec["name"].split(" ")[0] if rec["name"] else ""),
                    rec.get("size", ""),
                    rec.get("category", "Άλλο"),
                ),
            )
            cur = conn.execute(
                "INSERT OR IGNORE INTO price_snapshots (leaflet_id, product_key, price, unit_price, unit,"
                " promo_text, discount_pct, page, raw_text, snapshot_date, confidence, needs_review)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    leaflet_id,
                    key,
                    rec["price"],
                    rec.get("unit_price"),
                    rec.get("unit", ""),
                    rec.get("promo_text", ""),
                    rec.get("discount_pct"),
                    rec.get("page"),
                    rec.get("raw_text", ""),
                    snapshot_date,
                    rec.get("confidence", 0.0),
                    (
                        0
                        if rec.get("confidence", 0.0)
                        >= self.valves.min_parse_confidence
                        else 1
                    ),
                ),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
        return inserted
    # ------------------------------------------------------------------ retrieval
    def _leaflet_clause(self, conn: sqlite3.Connection) -> str:
        today = date.today().isoformat()
        rows = conn.execute(
            "SELECT leaflet_id FROM leaflets WHERE valid_to >= ?", (today,)
        ).fetchall()
        ids = [r["leaflet_id"] for r in rows]
        if not ids:
            latest = conn.execute(
                "SELECT leaflet_id FROM leaflets ORDER BY valid_from DESC LIMIT 1"
            ).fetchall()
            ids = [r["leaflet_id"] for r in latest]
        if not ids:
            return "''"
        return ",".join("'" + i.replace("'", "''") + "'" for i in ids)
    def _query(self, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
        conn = self._db()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    def _guard_db(self) -> Optional[str]:
        if not os.path.exists(self.valves.db_path):
            return (
                "Store not initialised: no database at db_path. Run refresh_leaflets() first, "
                "and check the db_path Valve points at a mounted volume."
            )
        return None
    async def list_leaflets(self, __event_emitter__: Optional[Callable] = None) -> str:
        """List leaflets in the local store with validity windows and row counts.
        :return: compact table (one line per leaflet); bounded by the token budget.
        """
        guard = self._guard_db()
        if guard:
            return guard
        rows = self._query(
            "SELECT l.leaflet_id, l.valid_from, l.valid_to, l.pages, l.extraction_method, l.source_url,"
            " COUNT(s.snapshot_id) AS n, ROUND(AVG(s.confidence),2) AS conf"
            " FROM leaflets l LEFT JOIN price_snapshots s ON s.leaflet_id=l.leaflet_id"
            " GROUP BY l.leaflet_id ORDER BY l.valid_from DESC"
        )
        if not rows:
            return "Store is empty. Run refresh_leaflets() first."
        today = date.today().isoformat()
        table_rows = []
        for r in rows:
            state = "current" if r["valid_to"] >= today else "past"
            table_rows.append(
                [
                    r["leaflet_id"],
                    r["valid_from"],
                    r["valid_to"],
                    state,
                    str(r["pages"] or 0),
                    str(r["n"]),
                    str(r["conf"] or ""),
                    r["extraction_method"] or "",
                ]
            )
        text = (
            "Leaflets in store ("
            + self.valves.db_path
            + "):\n"
            + self._format_table(
                [
                    "leaflet",
                    "valid_from",
                    "valid_to",
                    "state",
                    "pages",
                    "rows",
                    "conf",
                    "method",
                ],
                table_rows,
            )
        )
        await self._cite(__event_emitter__, "Lidl leaflet store", rows)
        return self._truncate_to_budget(text, len(table_rows), len(table_rows))
    @staticmethod
    def _query_tokens(query: str) -> List[str]:
        folded = re.sub(r"[^α-ωa-z0-9 ]+", " ", _fold(query))
        return [t for t in folded.split() if len(t) >= 2]
    def _match_products(self, query: str, limit: int = 5) -> List[Tuple[str, str]]:
        tokens = self._query_tokens(query)
        if not tokens:
            return []
        where = " AND ".join(["p.product_key LIKE ?"] * len(tokens))
        params = tuple(f"%{t}%" for t in tokens)
        rows = self._query(
            f"SELECT p.product_key, p.display_name, COUNT(s.snapshot_id) AS n"
            f" FROM products p LEFT JOIN price_snapshots s ON s.product_key=p.product_key"
            f" WHERE {where} AND COALESCE(s.needs_review,0)=0"
            f" GROUP BY p.product_key ORDER BY n DESC, length(p.display_name) ASC LIMIT ?",
            params + (limit,),
        )
        return [(r["product_key"], r["display_name"]) for r in rows]
    async def search_products(
        self,
        query: str,
        category: str = "",
        limit: int = 20,
        include_needs_review: bool = False,
        __event_emitter__: Optional[Callable] = None,
    ) -> str:
        """Search current-leaflet offers by Greek-aware, accent-insensitive name match.
        :param query: product text, e.g. "ελαιόλαδο" or "κασερόπιτα" (accents optional).
        :param category: optional category filter (substring, accent-insensitive).
        :param limit: max rows before the token budget cuts in (default 20).
        :return: table product | price | unit | promo | valid | page; <= ~800 tokens.
        """
        guard = self._guard_db()
        if guard:
            return guard
        tokens = self._query_tokens(query)
        if not tokens:
            return "Empty query: pass at least two characters, e.g. search_products('ελαιόλαδο')."
        where = " AND ".join(["s.product_key LIKE ?"] * len(tokens))
        params: List[Any] = [f"%{t}%" for t in tokens]
        clause = self._leaflet_clause(self._db())
        sql = (
            f"SELECT p.display_name, p.category, s.price, s.unit_price, s.unit, s.promo_text,"
            f" s.discount_pct, s.page, s.confidence, s.needs_review, l.valid_from, l.valid_to,"
            f" l.source_url, l.leaflet_id"
            f" FROM price_snapshots s JOIN products p ON p.product_key=s.product_key"
            f" JOIN leaflets l ON l.leaflet_id=s.leaflet_id"
            f" WHERE s.leaflet_id IN ({clause}) AND {where}"
        )
        if not include_needs_review:
            sql += " AND s.needs_review=0"
        if category:
            sql += " AND p.category LIKE ?"
            params.append(f"%{_fold(category)}%")
        sql += " ORDER BY s.price ASC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        rows = self._query(sql, tuple(params))
        if not rows:
            return (
                f"No offers matched '{query}' in the current leaflet(s). "
                f"Try a shorter Greek stem (e.g. 'ελαι', 'κασερ') or call list_leaflets() "
                f"to confirm a current leaflet is in the store."
            )
        table = [
            [
                r["display_name"][:42],
                f"{r['price']:.2f}",
                (
                    f"{r['unit_price']:.2f}/{r['unit']}"
                    if r["unit_price"] and r["unit"]
                    else (f"{r['unit_price']:.2f}" if r["unit_price"] else "-")
                ),
                (
                    r["promo_text"]
                    or (
                        "-"
                        if r["discount_pct"] is None
                        else f"{r['discount_pct']:.0f}%"
                    )
                )[:28],
                f"{r['valid_from']}→{r['valid_to']}",
                str(r["page"]),
            ]
            for r in rows
        ]
        text = (
            f"Current offers matching '{query}' (cheapest first, {len(rows)} row(s)):\n"
            + self._format_table(
                ["product", "price €", "unit", "promo", "valid", "p"], table
            )
            + f"\nSource: {rows[0]['source_url']}"
        )
        await self._cite(__event_emitter__, f"search_products('{query}')", rows)
        return self._truncate_to_budget(
            text, len(table), len(table), "add a keyword or lower limit"
        )
    async def get_price_history(
        self,
        product: str,
        weeks: int = 26,
        __event_emitter__: Optional[Callable] = None,
    ) -> str:
        """Price-over-time for the best-matching product across stored snapshots.
        :param product: product text to match (accent-insensitive Greek).
        :param weeks: look-back window in weeks (default 26).
        :return: table snapshot_date | leaflet | price | promo | page plus first/last/min/max deltas; <= ~800 tokens.
        """
        guard = self._guard_db()
        if guard:
            return guard
        matches = self._match_products(product, limit=3)
        if not matches:
            return (
                f"No stored product matches '{product}'. Run refresh_leaflets() to ingest the current "
                f"leaflet, or try a shorter stem."
            )
        key, display = matches[0]
        since = (date.today() - timedelta(weeks=max(1, int(weeks)))).isoformat()
        rows = self._query(
            "SELECT s.price, s.unit_price, s.unit, s.promo_text, s.page, s.snapshot_date, s.confidence,"
            " l.leaflet_id, l.valid_to FROM price_snapshots s JOIN leaflets l ON l.leaflet_id=s.leaflet_id"
            " WHERE s.product_key=? AND s.snapshot_date>=? AND s.needs_review=0"
            " ORDER BY s.snapshot_date ASC",
            (key, since),
        )
        if not rows:
            return (
                f"'{display}' has no snapshot rows since {since}. Widen weeks (e.g. weeks=104) "
                f"or ingest more leaflets."
            )
        prices = [r["price"] for r in rows]
        table = [
            [
                r["snapshot_date"],
                r["leaflet_id"][-8:],
                f"{r['price']:.2f}",
                (r["promo_text"] or "-")[:26],
                str(r["page"]),
            ]
            for r in rows
        ]
        delta = prices[-1] - prices[0]
        pct = (delta / prices[0] * 100) if prices[0] else 0.0
        stats = (
            f"first {prices[0]:.2f}€ → last {prices[-1]:.2f}€ (Δ {delta:+.2f}€, {pct:+.1f}%); "
            f"min {min(prices):.2f}€, max {max(prices):.2f}€ over {len(rows)} snapshot(s)"
        )
        head = f"Price history for '{display}' (product_key={key[:60]})"
        if len(matches) > 1:
            head += "\nOther matches: " + ", ".join(m[1][:28] for m in matches[1:])
        text = (
            head
            + "\n"
            + self._format_table(["date", "leaflet", "price €", "promo", "p"], table)
            + "\n"
            + stats
        )
        await self._cite(__event_emitter__, f"get_price_history('{product}')", rows)
        return self._truncate_to_budget(text, len(table), len(table), "reduce weeks")
    async def get_deals(
        self,
        category: str = "",
        min_discount: float = 0.0,
        limit: int = 20,
        __event_emitter__: Optional[Callable] = None,
    ) -> str:
        """Current promotions (percentage badges / Lidl Plus / multi-buy) from the latest leaflet(s).
        :param category: optional category substring filter.
        :param min_discount: minimum absolute discount percentage (e.g. 25).
        :param limit: max rows before the token budget cuts in.
        :return: table product | price | discount | promo | page; <= ~800 tokens.
        """
        guard = self._guard_db()
        if guard:
            return guard
        clause = self._leaflet_clause(self._db())
        sql = (
            "SELECT p.display_name, p.category, s.price, s.discount_pct, s.promo_text, s.page,"
            " s.confidence, l.valid_from, l.valid_to, l.source_url, l.leaflet_id, s.product_key"
            " FROM price_snapshots s JOIN products p ON p.product_key=s.product_key"
            " JOIN leaflets l ON l.leaflet_id=s.leaflet_id"
            f" WHERE s.leaflet_id IN ({clause}) AND s.needs_review=0"
            " AND (COALESCE(s.discount_pct,0) >= ? OR (s.promo_text IS NOT NULL AND s.promo_text<>''))"
        )
        params: List[Any] = [float(min_discount)]
        if category:
            sql += " AND p.category LIKE ?"
            params.append(f"%{_fold(category)}%")
        sql += " ORDER BY COALESCE(s.discount_pct,0) DESC, s.price ASC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        rows = self._query(sql, tuple(params))
        if not rows:
            return (
                f"No promotions found for min_discount={min_discount} in the current leaflet(s). "
                f"Lower min_discount or run refresh_leaflets() first."
            )
        table = [
            [
                r["display_name"][:40],
                f"{r['price']:.2f}",
                (f"{r['discount_pct']:.0f}%" if r["discount_pct"] is not None else "-"),
                (r["promo_text"] or "-")[:30],
                str(r["page"]),
            ]
            for r in rows
        ]
        text = (
            f"Current deals ({len(rows)} row(s), min_discount={min_discount}):\n"
            + self._format_table(["product", "price €", "off", "promo", "p"], table)
            + f"\nSource: {rows[0]['source_url']}"
        )
        await self._cite(__event_emitter__, "get_deals", rows)
        return self._truncate_to_budget(
            text, len(table), len(table), "raise min_discount or set a category"
        )
    async def check_seasonal_item(
        self, product: str, __event_emitter__: Optional[Callable] = None
    ) -> str:
        """Check whether a once-a-year / weekly-special item appears in stored snapshots.
        :param product: product text (accent-insensitive Greek).
        :return: presence table snapshot_date | leaflet | price | page plus appearance count; <= ~800 tokens.
        """
        guard = self._guard_db()
        if guard:
            return guard
        matches = self._match_products(product, limit=3)
        if not matches:
            return (
                f"'{product}' has never appeared in the store. If you expect it in a past leaflet, "
                f"ingest that week first (refresh_leaflets after the leaflet goes live)."
            )
        key, display = matches[0]
        rows = self._query(
            "SELECT s.snapshot_date, s.price, s.page, s.promo_text, l.leaflet_id, l.valid_from, l.valid_to"
            " FROM price_snapshots s JOIN leaflets l ON l.leaflet_id=s.leaflet_id"
            " WHERE s.product_key=? AND s.needs_review=0 ORDER BY s.snapshot_date ASC",
            (key,),
        )
        if not rows:
            return f"'{display}' matches only low-confidence rows; nothing to report."
        table = [
            [
                r["snapshot_date"],
                r["leaflet_id"][-8:],
                f"{r['price']:.2f}",
                (r["promo_text"] or "-")[:26],
                str(r["page"]),
            ]
            for r in rows
        ]
        span = len({r["snapshot_date"][:7] for r in rows})
        verdict = (
            "appears in a single month — treat as seasonal/one-off"
            if span <= 1 and len(rows) <= 2
            else f"appears across {span} month(s)"
        )
        text = (
            f"Seasonal check for '{display}': {len(rows)} appearance(s), {verdict}\n"
            + self._format_table(["date", "leaflet", "price €", "promo", "p"], table)
            + "\nNote: presence is name-matching only; renamed packagings can be missed."
        )
        await self._cite(__event_emitter__, f"check_seasonal_item('{product}')", rows)
        return self._truncate_to_budget(text, len(table), len(table))
    async def export_current_leaflet(
        self,
        format: str = "json",
        max_rows: int = 500,
        __event_emitter__: Optional[Callable] = None,
    ) -> str:
        """Escape hatch for bulk analysis: current leaflet rows as JSON or markdown.
        :param format: "json" (default) or "markdown".
        :param max_rows: hard cap on rows (also clamped by the token budget).
        :return: JSON array or markdown table, always truncated to the token budget.
        """
        guard = self._guard_db()
        if guard:
            return guard
        clause = self._leaflet_clause(self._db())
        rows = self._query(
            "SELECT p.display_name, p.category, s.price, s.unit_price, s.unit, s.discount_pct,"
            " s.promo_text, s.page, s.confidence, s.snapshot_date, l.leaflet_id, l.valid_from,"
            " l.valid_to, l.source_url FROM price_snapshots s"
            " JOIN products p ON p.product_key=s.product_key JOIN leaflets l ON l.leaflet_id=s.leaflet_id"
            f" WHERE s.leaflet_id IN ({clause}) AND s.needs_review=0 ORDER BY s.page, s.price DESC LIMIT ?",
            (max(1, min(int(max_rows), 1000)),),
        )
        if not rows:
            return "Nothing to export: ingest a leaflet first."
        if _fold(format).startswith("md") or _fold(format).startswith("mark"):
            table = [
                [
                    r["display_name"][:38],
                    f"{r['price']:.2f}",
                    str(r["page"]),
                    (r["promo_text"] or "-")[:22],
                ]
                for r in rows
            ]
            text = f"Current leaflet rows ({len(rows)}):\n" + self._format_table(
                ["product", "price €", "p", "promo"], table
            )
            shown = len(table)
        else:
            payload = [
                {
                    "name": r["display_name"],
                    "category": r["category"],
                    "price": r["price"],
                    "unit_price": r["unit_price"],
                    "unit": r["unit"],
                    "discount_pct": r["discount_pct"],
                    "promo": r["promo_text"],
                    "page": r["page"],
                    "confidence": r["confidence"],
                    "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                    "source_url": r["source_url"],
                }
                for r in rows
            ]
            text = json.dumps(payload, ensure_ascii=False, indent=0)
            shown = len(payload)
        await self._cite(__event_emitter__, "export_current_leaflet", rows)
        return self._truncate_to_budget(text, shown, len(rows), "lower max_rows")

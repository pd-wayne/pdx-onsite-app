"""
db.py — SQLite order tracking for PDX Onsite
"""
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from typing import Optional

log = logging.getLogger("pdx.db")


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


DB_PATH = os.path.join(_app_dir(), "pdx_onsite.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_columns(conn, table: str, columns: list):
    """Add columns that may not exist yet; silently skips if already present."""
    for col, typedef in columns:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            conn.commit()
        except Exception:
            pass


def init_db():
    with get_conn() as conn:
        # ── Core tables ───────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                order_num        TEXT UNIQUE NOT NULL,
                customer_name    TEXT,
                gallery          TEXT,
                items_json       TEXT,
                images_json      TEXT,
                placed_at        TEXT,
                received_at      TEXT NOT NULL,
                confirmed_at     TEXT,
                fulfilled_at     TEXT,
                status           TEXT NOT NULL DEFAULT 'received',
                download_status  TEXT NOT NULL DEFAULT 'pending',
                download_error   TEXT,
                fulfill_status   TEXT NOT NULL DEFAULT 'unfulfilled',
                fulfillment_mode TEXT,
                raw_json         TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                gallery          TEXT UNIQUE NOT NULL,
                first_seen       TEXT NOT NULL,
                last_seen        TEXT NOT NULL,
                order_count      INTEGER DEFAULT 0,
                fulfillment_mode TEXT NOT NULL DEFAULT 'onsite',
                show_dropship    INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                level   TEXT NOT NULL DEFAULT 'info',
                message TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # ── Routing tables ────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS destinations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                hot_folder_path TEXT NOT NULL,
                is_default      INTEGER NOT NULL DEFAULT 0,
                active          INTEGER NOT NULL DEFAULT 1,
                last_success_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_routing (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                print_spec     TEXT NOT NULL UNIQUE,
                destination_id INTEGER REFERENCES destinations(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id       INTEGER NOT NULL REFERENCES orders(id),
                filename       TEXT NOT NULL,
                print_spec     TEXT NOT NULL,
                destination_id INTEGER NOT NULL REFERENCES destinations(id),
                status         TEXT NOT NULL DEFAULT 'queued',
                printed_at     TEXT
            )
        """)

        # ── Indexes ───────────────────────────────────────────────────────────
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status     ON orders(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gallery    ON orders(gallery)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_order ON order_items(order_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON order_items(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_routing_spec ON product_routing(print_spec)")
        conn.commit()

        # ── Migrations for existing installs ──────────────────────────────────
        _migrate_columns(conn, "orders", [
            ("fulfilled_at",    "TEXT"),
            ("fulfill_status",  "TEXT NOT NULL DEFAULT 'unfulfilled'"),
            ("images_json",     "TEXT"),
            ("fulfillment_mode", "TEXT"),
        ])
        _migrate_columns(conn, "jobs", [
            ("fulfillment_mode", "TEXT NOT NULL DEFAULT 'onsite'"),
            ("show_dropship",   "INTEGER NOT NULL DEFAULT 0"),
        ])


# ── Activity log ──────────────────────────────────────────────────────────────

def log_activity(message: str, level: str = "info"):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (ts, level, message) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), level, message)
        )
        conn.commit()


def get_activity_log(limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ts, level, message FROM activity_log ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]


# ── Orders ────────────────────────────────────────────────────────────────────

def upsert_order(order_data: dict) -> bool:
    """Insert order if not already seen. Returns True if new."""
    order_num = order_data.get("num") or order_data.get("order_num")
    if not order_num:
        return False

    shipping    = order_data.get("shipping", {})
    destination = shipping.get("destination", {})
    customer_name = destination.get("recipient", "Unknown")
    gallery   = order_data.get("gallery", "")
    placed_at = order_data.get("placedAt", "")

    # Per-order pickup/dropship classification
    fulfillment_mode = (
        "pickup" if shipping.get("option", {}).get("externalId", "") == "pdx_pickup"
        else "dropship"
    )

    items = order_data.get("items", [])
    images = []
    items_summary = []
    for idx, item in enumerate(items):
        item_images = item.get("images", [])
        image_count = len(item_images) if item_images else item.get("quantity", 1)
        qty = item.get("quantity", 1)
        items_summary.append({
            "sku":   item.get("externalId", ""),
            "desc":  item.get("description", ""),
            "qty":   qty,
            "files": image_count,
        })
        for img in item_images:
            asset_url = img.get("assetUrl", "")
            filename  = img.get("filename", "")
            if asset_url and filename:
                images.append({
                    "filename":   filename,
                    "assetUrl":   asset_url,
                    "item_sku":   item.get("externalId", ""),
                    "item_idx":   idx,
                    "item_qty":   qty,
                    "item_desc":  item.get("description", ""),
                    "print_spec": img.get("externalId", ""),
                })

    pdx_status = order_data.get("status", "received").lower()
    if pdx_status not in ("received", "fulfilled", "late"):
        pdx_status = "received"
    db_status = "fulfilled" if pdx_status == "fulfilled" else "received"

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, status FROM orders WHERE order_num = ?", (order_num,)
        ).fetchone()
        if existing:
            # Never auto-update status from PDX polling — only the scan flow confirms orders.
            return False

        conn.execute("""
            INSERT INTO orders
                (order_num, customer_name, gallery, items_json, images_json,
                 placed_at, received_at, status, download_status, fulfill_status,
                 fulfillment_mode, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'unfulfilled', ?, ?)
        """, (
            order_num, customer_name, gallery,
            json.dumps(items_summary), json.dumps(images),
            placed_at, datetime.now().isoformat(), db_status,
            fulfillment_mode, json.dumps(order_data)
        ))
        conn.commit()

    # Map order mode → job mode: pickup orders default to onsite jobs
    job_mode = "onsite" if fulfillment_mode == "pickup" else "in_studio"
    upsert_job(gallery, default_mode=job_mode)
    return True


def confirm_order(order_num: str) -> bool:
    with get_conn() as conn:
        result = conn.execute("""
            UPDATE orders SET status = 'fulfilled', confirmed_at = ?
            WHERE order_num = ? AND status IN ('received', 'ready')
        """, (datetime.now().isoformat(), order_num))
        conn.commit()
        return result.rowcount > 0


def set_fulfilled(order_num: str) -> bool:
    with get_conn() as conn:
        result = conn.execute("""
            UPDATE orders SET fulfill_status = 'fulfilled', fulfilled_at = ?
            WHERE order_num = ?
        """, (datetime.now().isoformat(), order_num))
        conn.commit()
        return result.rowcount > 0


def set_download_status(order_num: str, status: str, error: str = ""):
    with get_conn() as conn:
        conn.execute("""
            UPDATE orders SET download_status = ?, download_error = ?
            WHERE order_num = ?
        """, (status, error, order_num))
        conn.commit()


def get_images_json(order_num: str) -> list:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT images_json FROM orders WHERE order_num = ?", (order_num,)
        ).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                pass
        return []


def get_order(order_num: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_num = ?", (order_num,)
        ).fetchone()
        return dict(row) if row else None


def get_queue(gallery_filter: Optional[str] = None) -> list:
    with get_conn() as conn:
        if gallery_filter:
            rows = conn.execute("""
                SELECT * FROM orders
                WHERE status IN ('received', 'ready') AND gallery = ?
                ORDER BY received_at ASC
            """, (gallery_filter,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM orders WHERE status IN ('received', 'ready')
                ORDER BY received_at ASC
            """).fetchall()
        return [dict(r) for r in rows]


def get_history(gallery_filter: Optional[str] = None, limit: int = 500) -> list:
    with get_conn() as conn:
        if gallery_filter:
            rows = conn.execute("""
                SELECT * FROM orders WHERE status = 'fulfilled' AND gallery = ?
                ORDER BY received_at DESC
            """, (gallery_filter,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM orders WHERE status = 'fulfilled'
                ORDER BY received_at DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def search_orders(query: str, gallery_filter: Optional[str] = None, limit: int = 100) -> list:
    q = f"%{query}%"
    with get_conn() as conn:
        if gallery_filter:
            rows = conn.execute("""
                SELECT * FROM orders
                WHERE gallery = ?
                  AND (order_num LIKE ? OR customer_name LIKE ? OR images_json LIKE ?)
                ORDER BY received_at DESC LIMIT ?
            """, (gallery_filter, q, q, q, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM orders
                WHERE order_num LIKE ?
                   OR customer_name LIKE ?
                   OR images_json LIKE ?
                ORDER BY received_at DESC LIMIT ?
            """, (q, q, q, limit)).fetchall()
        return [dict(r) for r in rows]


def get_stats(gallery_filter: Optional[str] = None) -> dict:
    with get_conn() as conn:
        if gallery_filter:
            total     = conn.execute("SELECT COUNT(*) FROM orders WHERE gallery=?", (gallery_filter,)).fetchone()[0]
            received  = conn.execute("SELECT COUNT(*) FROM orders WHERE status='received' AND gallery=?", (gallery_filter,)).fetchone()[0]
            ready     = conn.execute("SELECT COUNT(*) FROM orders WHERE status='ready' AND gallery=?", (gallery_filter,)).fetchone()[0]
            confirmed = conn.execute("SELECT COUNT(*) FROM orders WHERE status='fulfilled' AND gallery=?", (gallery_filter,)).fetchone()[0]
            fulfilled = conn.execute("SELECT COUNT(*) FROM orders WHERE fulfill_status='fulfilled' AND gallery=?", (gallery_filter,)).fetchone()[0]
        else:
            total     = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            received  = conn.execute("SELECT COUNT(*) FROM orders WHERE status='received'").fetchone()[0]
            ready     = conn.execute("SELECT COUNT(*) FROM orders WHERE status='ready'").fetchone()[0]
            confirmed = conn.execute("SELECT COUNT(*) FROM orders WHERE status='fulfilled'").fetchone()[0]
            fulfilled = conn.execute("SELECT COUNT(*) FROM orders WHERE fulfill_status='fulfilled'").fetchone()[0]
        return {"total": total, "pending": received, "ready": ready, "confirmed": confirmed, "fulfilled": fulfilled}


def get_all_galleries() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT gallery FROM orders WHERE gallery != '' ORDER BY gallery"
        ).fetchall()
        return [r[0] for r in rows]


def order_exists(order_num: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM orders WHERE order_num=?", (order_num,)).fetchone()
        return row is not None


# ── Jobs ──────────────────────────────────────────────────────────────────────

def upsert_job(gallery: str, default_mode: str = "onsite"):
    if not gallery:
        return
    now = datetime.now().isoformat()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM jobs WHERE gallery = ?", (gallery,)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE jobs SET last_seen = ?, order_count = order_count + 1
                WHERE gallery = ?
            """, (now, gallery))
        else:
            conn.execute("""
                INSERT INTO jobs (gallery, first_seen, last_seen, order_count, fulfillment_mode)
                VALUES (?, ?, ?, 1, ?)
            """, (gallery, now, now, default_mode))
        conn.commit()


def get_jobs() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT gallery, first_seen, last_seen, order_count, fulfillment_mode, show_dropship
            FROM jobs ORDER BY last_seen DESC
        """).fetchall()
        return [dict(r) for r in rows]


def get_job(gallery: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE gallery = ?", (gallery,)).fetchone()
        return dict(row) if row else None


def update_job_mode(gallery: str, mode: str, show_dropship: Optional[bool] = None):
    """Update a job's fulfillment_mode and/or show_dropship toggle."""
    with get_conn() as conn:
        if show_dropship is not None:
            conn.execute("""
                UPDATE jobs SET fulfillment_mode = ?, show_dropship = ? WHERE gallery = ?
            """, (mode, int(show_dropship), gallery))
        else:
            conn.execute(
                "UPDATE jobs SET fulfillment_mode = ? WHERE gallery = ?", (mode, gallery)
            )
        conn.commit()


def _rebuild_jobs_from_orders():
    """Build jobs table from existing orders (migration pass 4)."""
    with get_conn() as conn:
        galleries = conn.execute(
            "SELECT DISTINCT gallery, COUNT(*) as cnt FROM orders WHERE gallery != '' GROUP BY gallery"
        ).fetchall()
        now = datetime.now().isoformat()
        for row in galleries:
            gallery, cnt = row[0], row[1]
            existing = conn.execute("SELECT id FROM jobs WHERE gallery=?", (gallery,)).fetchone()
            if not existing:
                conn.execute("""
                    INSERT INTO jobs (gallery, first_seen, last_seen, order_count)
                    VALUES (?, ?, ?, ?)
                """, (gallery, now, now, cnt))
        conn.commit()


def migrate_fulfilled_orders(lab_id: str, api_key: str):
    """
    Migration to ensure all fulfilled PDX orders are stored with status='fulfilled'.
    Pass 1: Fix any orders stored as 'confirmed' → 'fulfilled'.
    Pass 2: Read raw_json to find orders where PDX status=fulfilled but stored as received.
    Pass 3: Intentionally removed — auto-syncing fulfilled status caused orders to silently
            complete without a physical scan.
    Pass 4: Rebuild jobs table from existing orders.
    """
    try:
        with get_conn() as conn:
            result = conn.execute(
                "UPDATE orders SET status = 'fulfilled' WHERE status = 'confirmed'"
            )
            conn.commit()
            if result.rowcount:
                log_activity(f"Migrated {result.rowcount} order(s): confirmed → fulfilled")
    except Exception as e:
        log.warning(f"[Migrate] pass1: {e}")

    try:
        updated = 0
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT order_num, raw_json FROM orders WHERE status = 'received' AND raw_json IS NOT NULL"
            ).fetchall()
            for row in rows:
                try:
                    raw = json.loads(row["raw_json"])
                    if raw.get("status", "").lower() == "fulfilled":
                        conn.execute(
                            "UPDATE orders SET status = 'fulfilled' WHERE order_num = ?",
                            (row["order_num"],)
                        )
                        updated += 1
                except Exception:
                    pass
            conn.commit()
        if updated:
            log_activity(f"Migrated {updated} order(s) via raw_json (pass 2)")
    except Exception as e:
        log.warning(f"[Migrate] pass2: {e}")

    _rebuild_jobs_from_orders()


# ── Destinations ──────────────────────────────────────────────────────────────

def seed_default_destination(output_folder: str):
    """Create a default destination from the legacy output folder config if none exist."""
    if not output_folder:
        return
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM destinations").fetchone()[0]
        if count == 0:
            conn.execute("""
                INSERT INTO destinations (name, hot_folder_path, is_default, active)
                VALUES ('Default', ?, 1, 1)
            """, (output_folder,))
            conn.commit()
            log.info(f"[DB] Seeded default destination from config: {output_folder}")


def get_destinations() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, name, hot_folder_path, is_default, active, last_success_at
            FROM destinations ORDER BY is_default DESC, name ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_default_destination() -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM destinations WHERE is_default = 1 AND active = 1 LIMIT 1"
        ).fetchone()
        if row:
            return dict(row)
        # Fall back to any active destination
        row = conn.execute(
            "SELECT * FROM destinations WHERE active = 1 LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def upsert_destination(name: str, hot_folder_path: str,
                       is_default: bool = False, active: bool = True,
                       dest_id: Optional[int] = None) -> int:
    with get_conn() as conn:
        if is_default:
            # Clear existing default before setting a new one
            conn.execute("UPDATE destinations SET is_default = 0")
        if dest_id:
            conn.execute("""
                UPDATE destinations
                SET name = ?, hot_folder_path = ?, is_default = ?, active = ?
                WHERE id = ?
            """, (name, hot_folder_path, int(is_default), int(active), dest_id))
            conn.commit()
            return dest_id
        cur = conn.execute("""
            INSERT INTO destinations (name, hot_folder_path, is_default, active)
            VALUES (?, ?, ?, ?)
        """, (name, hot_folder_path, int(is_default), int(active)))
        conn.commit()
        return cur.lastrowid


def delete_destination(dest_id: int) -> bool:
    """Remove a destination. Blocked if any order_items still reference it."""
    with get_conn() as conn:
        in_use = conn.execute(
            "SELECT COUNT(*) FROM order_items WHERE destination_id = ?", (dest_id,)
        ).fetchone()[0]
        if in_use:
            return False
        conn.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
        conn.execute(
            "UPDATE product_routing SET destination_id = NULL WHERE destination_id = ?", (dest_id,)
        )
        conn.commit()
        return True


def update_destination_health(destination_id: int):
    """Record a successful print timestamp for this destination."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE destinations SET last_success_at = ? WHERE id = ?",
            (datetime.now().isoformat(), destination_id)
        )
        conn.commit()


# ── Product routing ───────────────────────────────────────────────────────────

def get_routing() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT pr.id, pr.print_spec, pr.destination_id, d.name AS destination_name
            FROM product_routing pr
            LEFT JOIN destinations d ON pr.destination_id = d.id
            ORDER BY pr.print_spec ASC
        """).fetchall()
        return [dict(r) for r in rows]


def upsert_routing(print_spec: str, destination_id: Optional[int]) -> int:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM product_routing WHERE print_spec = ?", (print_spec,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE product_routing SET destination_id = ? WHERE print_spec = ?",
                (destination_id, print_spec)
            )
            conn.commit()
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO product_routing (print_spec, destination_id) VALUES (?, ?)",
            (print_spec, destination_id)
        )
        conn.commit()
        return cur.lastrowid


def get_destination_for_spec(print_spec: str) -> Optional[dict]:
    """Resolve a print_spec to its assigned destination. Falls back to default if unassigned."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT d.*
            FROM product_routing pr
            JOIN destinations d ON pr.destination_id = d.id
            WHERE pr.print_spec = ? AND d.active = 1
        """, (print_spec,)).fetchone()
        if row:
            return dict(row)
    return get_default_destination()


def discover_specs(specs: list) -> int:
    """Insert unknown print_specs as unassigned routing rows. Returns count added."""
    added = 0
    with get_conn() as conn:
        for spec in specs:
            if not spec:
                continue
            exists = conn.execute(
                "SELECT id FROM product_routing WHERE print_spec = ?", (spec,)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO product_routing (print_spec, destination_id) VALUES (?, NULL)",
                    (spec,)
                )
                added += 1
        conn.commit()
    return added


# ── Order items ───────────────────────────────────────────────────────────────

def insert_order_item(order_id: int, filename: str,
                      print_spec: str, destination_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO order_items (order_id, filename, print_spec, destination_id, status)
            VALUES (?, ?, ?, ?, 'queued')
        """, (order_id, filename, print_spec, destination_id))
        conn.commit()
        return cur.lastrowid


def update_item_status(item_id: int, status: str):
    """Set an order_item status to 'queued', 'printed', or 'error'."""
    printed_at = datetime.now().isoformat() if status == "printed" else None
    with get_conn() as conn:
        conn.execute(
            "UPDATE order_items SET status = ?, printed_at = ? WHERE id = ?",
            (status, printed_at, item_id)
        )
        conn.commit()


def get_order_items(order_num: str) -> list:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM orders WHERE order_num = ?", (order_num,)
        ).fetchone()
        if not row:
            return []
        items = conn.execute("""
            SELECT oi.id, oi.filename, oi.print_spec, oi.destination_id,
                   oi.status, oi.printed_at, d.name AS destination_name
            FROM order_items oi
            LEFT JOIN destinations d ON oi.destination_id = d.id
            WHERE oi.order_id = ?
            ORDER BY oi.id ASC
        """, (row["id"],)).fetchall()
        return [dict(i) for i in items]


def check_order_ready(order_num: str) -> bool:
    """
    If every order_item for this order is 'printed', promote order status to 'ready'.
    Returns True if the order just transitioned to ready.
    """
    with get_conn() as conn:
        order = conn.execute(
            "SELECT id, status FROM orders WHERE order_num = ?", (order_num,)
        ).fetchone()
        if not order or order["status"] != "received":
            return False

        total = conn.execute(
            "SELECT COUNT(*) FROM order_items WHERE order_id = ?", (order["id"],)
        ).fetchone()[0]
        if total == 0:
            return False

        pending = conn.execute("""
            SELECT COUNT(*) FROM order_items
            WHERE order_id = ? AND status != 'printed'
        """, (order["id"],)).fetchone()[0]

        if pending == 0:
            conn.execute(
                "UPDATE orders SET status = 'ready' WHERE id = ?", (order["id"],)
            )
            conn.commit()
            log.info(f"[DB] Order {order_num} → ready ({total} items printed)")
            return True
        return False


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row:
            try:
                return json.loads(row[0])
            except Exception:
                return row[0]
        return default


def set_setting(key: str, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value))
        )
        conn.commit()

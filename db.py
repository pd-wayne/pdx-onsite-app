"""
db.py — SQLite order tracking for PDX Onsite
"""
import sqlite3
import json
import os
import sys
from datetime import datetime
from typing import Optional

def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(_app_dir(), "pdx_onsite.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                order_num       TEXT UNIQUE NOT NULL,
                customer_name   TEXT,
                gallery         TEXT,
                items_json      TEXT,
                images_json     TEXT,
                placed_at       TEXT,
                received_at     TEXT NOT NULL,
                confirmed_at    TEXT,
                fulfilled_at    TEXT,
                status          TEXT NOT NULL DEFAULT 'received',
                download_status TEXT NOT NULL DEFAULT 'pending',
                download_error  TEXT,
                fulfill_status  TEXT NOT NULL DEFAULT 'unfulfilled',
                raw_json        TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                gallery      TEXT UNIQUE NOT NULL,
                first_seen   TEXT NOT NULL,
                last_seen    TEXT NOT NULL,
                order_count  INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT NOT NULL,
                level      TEXT NOT NULL DEFAULT 'info',
                message    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON orders(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gallery ON orders(gallery)")
        conn.commit()

        for col, typedef in [
            ("fulfilled_at",   "TEXT"),
            ("fulfill_status", "TEXT NOT NULL DEFAULT 'unfulfilled'"),
            ("images_json",    "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {typedef}")
                conn.commit()
            except Exception:
                pass


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


def upsert_order(order_data: dict) -> bool:
    """Insert order if not already seen. Returns True if new."""
    order_num = order_data.get("num") or order_data.get("order_num")
    if not order_num:
        return False

    shipping = order_data.get("shipping", {})
    destination = shipping.get("destination", {})
    customer_name = destination.get("recipient", "Unknown")
    gallery = order_data.get("gallery", "")
    placed_at = order_data.get("placedAt", "")

    items = order_data.get("items", [])
    images = []
    items_summary = []
    for item in items:
        item_images = item.get("images", [])
        image_count = len(item_images) if item_images else item.get("quantity", 1)
        items_summary.append({
            "sku":   item.get("externalId", ""),
            "desc":  item.get("description", ""),
            "qty":   item.get("quantity", 1),
            "files": image_count,
        })
        for img in item_images:
            asset_url = img.get("assetUrl", "")
            filename  = img.get("filename", "")
            if asset_url and filename:
                images.append({
                    "filename":  filename,
                    "assetUrl":  asset_url,
                    "item_sku":  item.get("externalId", ""),
                    "item_desc": item.get("description", ""),
                    "pose_id":   img.get("externalId", ""),
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
            # This prevents orders from silently completing without a physical scan.
            return False

        conn.execute("""
            INSERT INTO orders
                (order_num, customer_name, gallery, items_json, images_json,
                 placed_at, received_at, status, download_status, fulfill_status, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'unfulfilled', ?)
        """, (
            order_num, customer_name, gallery,
            json.dumps(items_summary), json.dumps(images),
            placed_at, datetime.now().isoformat(), db_status, json.dumps(order_data)
        ))
        conn.commit()

    upsert_job(gallery)
    return True


def confirm_order(order_num: str) -> bool:
    with get_conn() as conn:
        result = conn.execute("""
            UPDATE orders SET status = 'fulfilled', confirmed_at = ?
            WHERE order_num = ? AND status = 'received'
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
                SELECT * FROM orders WHERE status = 'received' AND gallery = ?
                ORDER BY received_at ASC
            """, (gallery_filter,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM orders WHERE status = 'received'
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
            confirmed = conn.execute("SELECT COUNT(*) FROM orders WHERE status='fulfilled' AND gallery=?", (gallery_filter,)).fetchone()[0]
            fulfilled = conn.execute("SELECT COUNT(*) FROM orders WHERE fulfill_status='fulfilled' AND gallery=?", (gallery_filter,)).fetchone()[0]
        else:
            total     = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            received  = conn.execute("SELECT COUNT(*) FROM orders WHERE status='received'").fetchone()[0]
            confirmed = conn.execute("SELECT COUNT(*) FROM orders WHERE status='fulfilled'").fetchone()[0]
            fulfilled = conn.execute("SELECT COUNT(*) FROM orders WHERE fulfill_status='fulfilled'").fetchone()[0]
        return {"total": total, "pending": received, "confirmed": confirmed, "fulfilled": fulfilled}


def get_all_galleries() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT gallery FROM orders WHERE gallery != '' ORDER BY gallery"
        ).fetchall()
        return [r[0] for r in rows]


# ── Jobs table ────────────────────────────────────────────────────────────────

def upsert_job(gallery: str):
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
                INSERT INTO jobs (gallery, first_seen, last_seen, order_count)
                VALUES (?, ?, ?, 1)
            """, (gallery, now, now))
        conn.commit()


def get_jobs() -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT gallery, first_seen, last_seen, order_count
            FROM jobs ORDER BY last_seen DESC
        """).fetchall()
        return [dict(r) for r in rows]


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
    Pass 3: Fetch fulfilled orders from PDX API as final backup.
    Pass 4: Rebuild jobs table from existing orders.
    """
    import api as pdx_api

    try:
        with get_conn() as conn:
            result = conn.execute(
                "UPDATE orders SET status = 'fulfilled' WHERE status = 'confirmed'"
            )
            conn.commit()
            if result.rowcount:
                log_activity(f"Migrated {result.rowcount} order(s): confirmed → fulfilled")
    except Exception as e:
        import logging as _logging
        _logging.getLogger("pdx.db").warning(f"[Migrate] pass1: {e}")

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
        import logging as _logging
        _logging.getLogger("pdx.db").warning(f"[Migrate] pass2: {e}")

    # Pass 3 (PDX API fetch) intentionally removed — auto-syncing fulfilled status
    # from PDX caused orders to silently complete without a physical scan.
    # Status is only set to 'fulfilled' via the confirm_order scan flow.

    _rebuild_jobs_from_orders()


def order_exists(order_num: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM orders WHERE order_num=?", (order_num,)).fetchone()
        return row is not None


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

"""
poller.py — Background polling thread for PDX Onsite
"""
import json
import threading
import time
import logging
from collections import defaultdict
from datetime import datetime

import api
import config
import db
import printer

log = logging.getLogger("pdx.poller")


class Poller:
    def __init__(self, on_new_orders=None, on_poll_complete=None, on_error=None, on_download_done=None):
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self.on_new_orders    = on_new_orders
        self.on_poll_complete = on_poll_complete
        self.on_error         = on_error
        self.on_download_done = on_download_done

        self.lab_id = ""
        self.api_key = ""
        self.interval = 60
        self.last_poll = None
        self.last_error = ""
        self.running = False
        self.next_poll_at = None

    def configure(self, lab_id: str, api_key: str, interval: int):
        with self._lock:
            self.lab_id = lab_id
            self.api_key = api_key
            self.interval = max(10, interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.running = True
        log.info("[Poller] Started")

    def stop(self):
        self._stop_event.set()
        self.running = False
        log.info("[Poller] Stopped")

    def trigger(self):
        self.next_poll_at = time.time()

    def _run(self):
        self.next_poll_at = time.time()
        while not self._stop_event.is_set():
            now = time.time()
            if now >= self.next_poll_at:
                self._do_poll()
                with self._lock:
                    self.next_poll_at = time.time() + self.interval
            time.sleep(1)

    def _do_poll(self):
        with self._lock:
            lab_id = self.lab_id
            api_key = self.api_key

        if not lab_id or not api_key:
            self.last_error = "No credentials configured"
            if self.on_error:
                self.on_error(self.last_error)
            return

        log.info("[Poller] Polling PDX...")
        orders, err = api.poll_orders(lab_id, api_key)
        self.last_poll = datetime.now().isoformat()

        if err:
            self.last_error = err
            log.warning(f"[Poller] Error: {err}")
            if self.on_error:
                self.on_error(err)
            return

        self.last_error = ""
        new_count = 0
        cfg = config.load()

        for order_data in orders:
            is_new = db.upsert_order(order_data)
            if not is_new:
                continue

            new_count += 1
            order_num = order_data.get("num") or order_data.get("order_num")
            gallery   = order_data.get("gallery", "")

            order    = db.get_order(order_num)
            job      = db.get_job(gallery) if gallery else None
            job_mode   = job["fulfillment_mode"] if job else cfg.get("app_mode", "onsite")
            order_mode = order["fulfillment_mode"] if order else "pickup"

            # Only pickup orders on onsite jobs get a receipt and auto-download.
            # Dropship orders on onsite jobs are stored only.
            # In-studio orders are handled by the packing slip workflow (Phase 6).
            if job_mode != "onsite" or order_mode != "pickup":
                log.info(f"[Poller] {order_num} ingested ({job_mode}/{order_mode}) — no receipt/download")
                continue

            self._print_receipt(order_num, order_data)

            # In manual mode the operator triggers download via "Send to Printer"
            if cfg.get("print_mode", "auto") != "manual":
                images   = db.get_images_json(order_num)
                order_id = order["id"] if order else None
                t = threading.Thread(
                    target=self._download_images,
                    args=(order_num, order_id, images, api_key),
                    daemon=True
                )
                t.start()

        log.info(f"[Poller] Poll complete — {new_count} new order(s)")

        if self.on_poll_complete:
            self.on_poll_complete(self.last_poll)
        if new_count > 0 and self.on_new_orders:
            self.on_new_orders(new_count)

    def _print_receipt(self, order_num: str, order_data: dict):
        cfg = config.load()
        printer_name = cfg.get("printer_name", "")
        studio_name  = cfg.get("studio_name", "")
        logo_path    = cfg.get("logo_path", "")

        if not printer_name:
            log.warning(f"[Poller] No printer configured — skipping receipt for {order_num}")
            return

        shipping    = order_data.get("shipping", {})
        destination = shipping.get("destination", {})

        stored = db.get_conn().execute(
            "SELECT items_json, images_json, received_at FROM orders WHERE order_num=?", (order_num,)
        ).fetchone()
        items_summary  = json.loads(stored["items_json"]) if stored and stored["items_json"] else []
        images_summary = stored["images_json"] if stored else "[]"

        order_dict = {
            "order_num":      order_num,
            "customer_name":  destination.get("recipient", "Unknown"),
            "customer_phone": destination.get("phone", ""),
            "gallery":        order_data.get("gallery", ""),
            "placed_at":      order_data.get("placedAt", ""),
            "received_at":    stored["received_at"] if stored else datetime.now().isoformat(),
            "items_json":     json.dumps(items_summary),
            "images_json":    images_summary,
        }

        try:
            ok, err = printer.print_receipt(order_dict, printer_name, studio_name, logo_path)
            if ok:
                log.info(f"[Poller] Receipt printed for {order_num}")
            else:
                log.warning(f"[Poller] Receipt print failed for {order_num}: {err}")
        except Exception as e:
            log.error(f"[Poller] Receipt exception for {order_num}: {e}")

    def _download_images(self, order_num: str, order_id, images: list, api_key: str):
        if not images:
            log.warning(f"[Download] No images for {order_num}")
            if self.on_download_done:
                self.on_download_done(order_num, True, "")
            return

        default_dest = db.get_default_destination()
        if not default_dest:
            err = "No destinations configured"
            log.warning(f"[Download] {err} — skipping {order_num}")
            db.set_download_status(order_num, "failed", err)
            if self.on_download_done:
                self.on_download_done(order_num, False, err)
            return

        log.info(f"[Download] Starting routed download for {order_num} ({len(images)} image(s))")

        # Group images by their resolved destination
        groups = defaultdict(list)
        for img in images:
            print_spec = img.get("print_spec", "")
            dest = db.get_destination_for_spec(print_spec)
            if not dest:
                dest = default_dest
            if dest["id"] == default_dest["id"] and print_spec:
                log.warning(f"[Download] Spec '{print_spec}' unassigned — routing to default destination")
            groups[dest["id"]].append((img, dest))

        overall_ok = True
        overall_err = ""
        item_ids_by_dest = defaultdict(list)

        for dest_id, img_dest_list in groups.items():
            dest   = img_dest_list[0][1]
            imgs   = [p[0] for p in img_dest_list]
            folder = dest["hot_folder_path"]

            if not folder:
                log.warning(f"[Download] Destination '{dest['name']}' has no path — skipping {len(imgs)} image(s)")
                continue

            # Create order_items rows before attempting download
            if order_id is not None:
                for img in imgs:
                    item_id = db.insert_order_item(
                        order_id, img["filename"], img.get("print_spec", ""), dest_id
                    )
                    item_ids_by_dest[dest_id].append(item_id)

            ok, err = printer.download_images(imgs, folder, order_num=order_num, api_key=api_key)

            if ok:
                if order_id is not None:
                    for item_id in item_ids_by_dest[dest_id]:
                        db.update_item_status(item_id, "printed")
                db.update_destination_health(dest_id)
                log.info(f"[Download] {len(imgs)} image(s) → '{dest['name']}' ({folder})")
            else:
                if order_id is not None:
                    for item_id in item_ids_by_dest[dest_id]:
                        db.update_item_status(item_id, "error")
                log.warning(f"[Download] Failed for dest '{dest['name']}': {err}")
                overall_ok = False
                overall_err = err

        if overall_ok:
            db.set_download_status(order_num, "ok")
            if order_id is not None and db.check_order_ready(order_num):
                log.info(f"[Download] {order_num} → ready for pickup")
        else:
            db.set_download_status(order_num, "failed", overall_err)

        if self.on_download_done:
            self.on_download_done(order_num, overall_ok, overall_err)

    def get_status(self) -> dict:
        with self._lock:
            seconds_until = max(0, int((self.next_poll_at or time.time()) - time.time()))
            return {
                "running":      self.running,
                "last_poll":    self.last_poll,
                "last_error":   self.last_error,
                "interval":     self.interval,
                "next_poll_in": seconds_until,
            }

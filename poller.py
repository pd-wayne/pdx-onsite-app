"""
poller.py — Background polling thread for PDX Onsite
"""
import json
import threading
import time
import logging
from datetime import datetime

import api
import config
import db
import printer

log = logging.getLogger("pdx.poller")


def _is_pickup_order(order_data: dict) -> bool:
    """Return True if the order's shipping option is an onsite pickup."""
    return order_data.get("shipping", {}).get("option", {}).get("externalId", "") == "pdx_pickup"


def _order_matches_mode(order_data: dict, mode: str) -> bool:
    """Return True if this order should be processed under the current fulfillment mode."""
    if mode == "both":
        return True
    is_pickup = _is_pickup_order(order_data)
    if mode == "dropship":
        return not is_pickup
    # default / "pickup"
    return is_pickup


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
        mode = config.load().get("fulfillment_mode", "pickup")

        for order_data in orders:
            if not _order_matches_mode(order_data, mode):
                continue
            is_new = db.upsert_order(order_data)
            gallery = order_data.get("gallery", "")
            if gallery:
                db.upsert_job(gallery)
            if not is_new:
                continue

            new_count += 1
            order_num = order_data.get("num") or order_data.get("order_num")

            self._print_receipt(order_num, order_data)

            # In manual mode, operator triggers download via "Send to Printer" button
            print_mode = config.load().get("print_mode", "auto")
            if print_mode != "manual":
                images = db.get_images_json(order_num)
                t = threading.Thread(
                    target=self._download_images,
                    args=(order_num, images, api_key),
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
            "order_num":     order_num,
            "customer_name": destination.get("recipient", "Unknown"),
            "gallery":       order_data.get("gallery", ""),
            "placed_at":     order_data.get("placedAt", ""),
            "received_at":   stored["received_at"] if stored else datetime.now().isoformat(),
            "items_json":    json.dumps(items_summary),
            "images_json":   images_summary,
        }

        try:
            ok, err = printer.print_receipt(order_dict, printer_name, studio_name, logo_path)
            if ok:
                log.info(f"[Poller] Receipt printed for {order_num}")
            else:
                log.warning(f"[Poller] Receipt print failed for {order_num}: {err}")
        except Exception as e:
            log.error(f"[Poller] Receipt exception for {order_num}: {e}")

    def _download_images(self, order_num: str, images: list, api_key: str):
        cfg = config.load()
        output_folder = cfg.get("image_output_folder", "")

        if not output_folder:
            log.warning(f"[Download] No output folder configured — skipping {order_num}")
            db.set_download_status(order_num, "failed", "No image output folder configured")
            if self.on_download_done:
                self.on_download_done(order_num, False, "No image output folder configured")
            return

        log.info(f"[Download] Starting download for {order_num} ({len(images)} image(s))")
        ok, err = printer.download_images(images, output_folder, order_num=order_num, api_key=api_key)

        if ok:
            db.set_download_status(order_num, "ok")
            log.info(f"[Download] Complete for {order_num}")
        else:
            db.set_download_status(order_num, "failed", err)
            log.warning(f"[Download] Failed for {order_num}: {err}")

        if self.on_download_done:
            self.on_download_done(order_num, ok, err)

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

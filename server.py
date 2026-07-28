"""
server.py — Flask app for PDX Onsite
"""
import json
import logging
import mimetypes
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog
from typing import Optional

from flask import Flask, Response, jsonify, redirect, request, send_from_directory, send_file

import api as pdx_api
import config
import db
import printer

log = logging.getLogger("pdx.server")
_event_queue: queue.Queue = queue.Queue(maxsize=200)
SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
MAX_LOGO_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
_pending_update: Optional[dict] = None


def set_pending_update(info: dict):
    global _pending_update
    _pending_update = info


def push_event(event: str, data: dict):
    try:
        payload = json.dumps({"event": event, "data": data})
        _event_queue.put_nowait(f"data: {payload}\n\n")
    except queue.Full:
        pass


def _log(msg: str, level: str = "info"):
    db.log_activity(msg, level)
    push_event("activity", {"message": msg, "level": level})
    getattr(log, level, log.info)(msg)


def create_app(poller, ui_path: str) -> Flask:
    app = Flask(__name__, static_folder=None)

    # Seed default destination from config on startup (no-op if destinations exist)
    cfg = config.load()
    if cfg.get("image_output_folder"):
        db.seed_default_destination(cfg["image_output_folder"])

    poller.on_new_orders    = lambda count:      (push_event("new_orders", {"count": count}), _log(f"📦 {count} new order(s) received"))
    poller.on_poll_complete = lambda ts:         push_event("poll_complete", {"timestamp": ts})
    poller.on_error         = lambda err:        (push_event("poll_error", {"error": err}), _log(f"Poll error: {err}", "error"))
    poller.on_download_done = lambda num, ok, e: (push_event("download_done", {"order_num": num, "ok": ok, "error": e}),
                                                  _log(f"Download {'complete' if ok else 'failed'}: {num}" + (f" — {e}" if not ok else "")))
    poller.on_order_ready   = lambda num: (push_event("order_state_change", {"order_num": num, "status": "ready"}),
                                           _log(f"✅ Order {num} ready for pickup"))

    # ── Frontend ──────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return send_from_directory(ui_path, "index.html")

    @app.route("/static/<path:filename>")
    def static_files(filename):
        return send_from_directory(ui_path, filename)

    # ── SSE ───────────────────────────────────────────────────────────────────

    @app.route("/api/events")
    def sse_stream():
        def generate():
            yield "data: {\"event\":\"connected\"}\n\n"
            while True:
                try:
                    msg = _event_queue.get(timeout=15)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ── Settings ──────────────────────────────────────────────────────────────

    @app.route("/api/get_settings")
    def get_settings():
        return jsonify(config.load())

    @app.route("/api/save_settings", methods=["POST"])
    def save_settings():
        try:
            data = request.get_json()
            ok = config.save(data)
            if ok:
                poller.configure(data.get("lab_id", ""), data.get("api_key", ""), int(data.get("poll_interval", 60)))
                if data.get("lab_id") and data.get("api_key") and not poller.running:
                    poller.start()
                if data.get("image_output_folder"):
                    db.seed_default_destination(data["image_output_folder"])
                # Seed jobs from historical orders in background
                if data.get("lab_id") and data.get("api_key"):
                    threading.Thread(target=_seed_jobs_background,
                                     args=(data["lab_id"], data["api_key"]), daemon=True).start()
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": "Failed to save"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/test_connection", methods=["POST"])
    def test_connection():
        data = request.get_json()
        ok, msg = pdx_api.test_connection(data.get("lab_id", ""), data.get("api_key", ""))
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/get_printers")
    def get_printers():
        try:
            return jsonify(printer.get_windows_printers() or [])
        except Exception:
            return jsonify([])

    @app.route("/api/upload_logo", methods=["POST"])
    def upload_logo():
        if "file" not in request.files:
            return jsonify({"ok": False, "error": "No file provided"})
        f = request.files["file"]
        if not f.filename:
            return jsonify({"ok": False, "error": "No filename"})
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".bmp"):
            return jsonify({"ok": False, "error": "Unsupported file type — use PNG or JPG"})

        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if size > MAX_LOGO_SIZE_BYTES:
            return jsonify({"ok": False, "error": f"Logo must be under {MAX_LOGO_SIZE_BYTES // (1024*1024)}MB"})

        from config import _app_dir
        logo_path = os.path.join(_app_dir(), f"studio_logo{ext}")
        try:
            f.save(logo_path)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})
        cfg = config.load()
        cfg["logo_path"] = logo_path
        config.save(cfg)
        _log(f"🖼 Logo uploaded: {os.path.basename(logo_path)}")
        return jsonify({"ok": True, "path": logo_path})

    @app.route("/api/get_logo")
    def get_logo():
        cfg = config.load()
        logo_path = cfg.get("logo_path", "")
        if not logo_path or not os.path.exists(logo_path):
            return "No logo", 404
        mime = mimetypes.guess_type(logo_path)[0] or "image/png"
        return send_file(logo_path, mimetype=mime)

    @app.route("/api/browse_folder")
    def browse_folder():
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(title="Select Folder")
            root.destroy()
            if path:
                return jsonify({"ok": True, "path": os.path.normpath(path)})
            return jsonify({"ok": False, "path": ""})
        except Exception as e:
            return jsonify({"ok": False, "path": "", "error": str(e)})

    # ── Jobs ──────────────────────────────────────────────────────────────────

    @app.route("/api/get_jobs")
    def get_jobs():
        return jsonify(db.get_jobs())

    @app.route("/api/fetch_job_history", methods=["POST"])
    def fetch_job_history():
        """Fetch full order history for a specific job from PDX API."""
        data = request.get_json()
        gallery = data.get("gallery", "")
        if not gallery:
            return jsonify({"ok": False, "error": "No gallery specified"})

        cfg = config.load()
        lab_id  = cfg.get("lab_id", "")
        api_key = cfg.get("api_key", "")

        def _fetch():
            try:
                orders, err = pdx_api.fetch_all_orders_for_job(lab_id, api_key, gallery)
                if err:
                    push_event("job_history_done", {"gallery": gallery, "ok": False, "error": err})
                    return
                new_count = 0
                for order_data in orders:
                    if db.upsert_order(order_data):
                        new_count += 1
                _log(f"📂 Loaded {new_count} new orders for '{gallery}'")
                push_event("job_history_done", {"gallery": gallery, "ok": True, "count": new_count})
            except Exception as e:
                log.warning(f"[JobHistory] Fetch failed for '{gallery}': {e}")
                push_event("job_history_done", {"gallery": gallery, "ok": False, "error": str(e)})

        threading.Thread(target=_fetch, daemon=True).start()
        return jsonify({"ok": True, "message": "Fetching job history…"})

    # ── Destinations ──────────────────────────────────────────────────────────

    @app.route("/api/get_destinations")
    def get_destinations():
        return jsonify(db.get_destinations())

    @app.route("/api/set_primary_destination_name", methods=["POST"])
    def set_primary_destination_name():
        """Backs the simple "Printer Name" field in Settings > Folders — names
        the sole/default destination without requiring the advanced multi-
        destination UI. Seeds one first if none exist yet."""
        data = request.get_json() or {}
        name = (data.get("name") or "").strip() or "Printer 1"
        cfg = config.load()
        db.seed_default_destination(cfg.get("image_output_folder", ""), name=name)
        db.set_primary_destination_name(name)
        return jsonify({"ok": True, "name": name})

    @app.route("/api/save_destination", methods=["POST"])
    def save_destination():
        data = request.get_json()
        try:
            dest_id = db.upsert_destination(
                name=data["name"].strip(),
                hot_folder_path=data["hot_folder_path"].strip(),
                is_default=bool(data.get("is_default", False)),
                active=bool(data.get("active", True)),
                dest_id=data.get("id") or None,
            )
            return jsonify({"ok": True, "id": dest_id})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/delete_destination", methods=["POST"])
    def delete_destination():
        data = request.get_json()
        ok = db.delete_destination(data.get("id"))
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Destination is in use by existing order items and cannot be deleted"})

    @app.route("/api/browse_folder_dest")
    def browse_folder_dest():
        """Same as browse_folder but used for destination path selection."""
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(title="Select Destination Folder")
            root.destroy()
            if path:
                return jsonify({"ok": True, "path": os.path.normpath(path)})
            return jsonify({"ok": False, "path": ""})
        except Exception as e:
            return jsonify({"ok": False, "path": "", "error": str(e)})

    # ── Product routing ────────────────────────────────────────────────────────

    @app.route("/api/get_routing")
    def get_routing():
        return jsonify(db.get_routing())

    @app.route("/api/save_routing", methods=["POST"])
    def save_routing():
        data = request.get_json()
        try:
            row_id = db.upsert_routing(
                print_spec=data["print_spec"],
                destination_id=data.get("destination_id") or None,
            )
            return jsonify({"ok": True, "id": row_id})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/discover_specs", methods=["POST"])
    def discover_specs_endpoint():
        cfg = config.load()
        lab_id  = cfg.get("lab_id", "")
        api_key = cfg.get("api_key", "")
        if not lab_id or not api_key:
            return jsonify({"ok": False, "error": "No credentials configured"})
        orders, err = pdx_api.poll_orders(lab_id, api_key)
        if err:
            return jsonify({"ok": False, "error": err})
        specs = {}
        for order in orders:
            for item in order.get("items", []):
                description = item.get("description", "")
                for img in item.get("images", []):
                    spec = img.get("externalId", "")
                    if spec and spec not in specs:
                        specs[spec] = description
        added = db.discover_specs(specs)
        return jsonify({"ok": True, "found": len(specs), "added": added})

    @app.route("/api/get_products_for_job")
    def get_products_for_job():
        gallery = request.args.get("gallery", "")
        if not gallery:
            return jsonify([])
        return jsonify(db.get_products_for_gallery(gallery))

    # ── Job mode ───────────────────────────────────────────────────────────────

    @app.route("/api/update_job_mode", methods=["POST"])
    def update_job_mode():
        data = request.get_json()
        gallery      = data.get("gallery", "")
        mode         = data.get("mode", "onsite")
        show_dropship = data.get("show_dropship")
        if not gallery:
            return jsonify({"ok": False, "error": "No gallery specified"})
        db.update_job_mode(gallery, mode,
                           show_dropship=None if show_dropship is None else bool(show_dropship))
        return jsonify({"ok": True})

    # ── Queue / History / Search ───────────────────────────────────────────────

    @app.route("/api/get_queue")
    def get_queue():
        gallery = request.args.get("gallery", "") or None
        return jsonify(db.get_queue(gallery))

    @app.route("/api/get_history")
    def get_history():
        gallery = request.args.get("gallery", "") or None
        return jsonify(db.get_history(gallery))

    @app.route("/api/get_stats")
    def get_stats():
        gallery = request.args.get("gallery", "") or None
        return jsonify(db.get_stats(gallery))

    @app.route("/api/get_galleries")
    def get_galleries():
        return jsonify(db.get_all_galleries())

    @app.route("/api/search")
    def search():
        query   = request.args.get("q", "").strip()
        gallery = request.args.get("gallery", "") or None
        if not query:
            return jsonify([])
        return jsonify(db.search_orders(query, gallery_filter=gallery))

    @app.route("/api/get_order")
    def get_order():
        order_num = request.args.get("order_num", "")
        order = db.get_order(order_num)
        if not order:
            return jsonify({"error": "Not found"}), 404
        return jsonify(order)

    # ── Order Actions ─────────────────────────────────────────────────────────

    @app.route("/api/confirm_order", methods=["POST"])
    def confirm_order():
        data = request.get_json()
        order_num = data.get("order_num", "")
        cfg = config.load()
        ok, err = pdx_api.shipped_callback(cfg.get("lab_id", ""), cfg.get("api_key", ""), order_num)
        if not ok:
            _log(f"Confirm failed for {order_num}: {err}", "error")
            return jsonify({"ok": False, "error": err})
        db.confirm_order(order_num)
        _log(f"✅ Confirmed (scanned): {order_num}")
        push_event("order_confirmed", {"order_num": order_num})
        return jsonify({"ok": True})

    VALID_CARRIERS = {"UPS", "UPSMI", "FEDEX", "USPS", "DHL", "PICKUP"}

    @app.route("/api/mark_shipped", methods=["POST"])
    def mark_shipped():
        """For non-pickup orders (dropship or bulk-ship): tell PDX the order has
        actually shipped, with a real carrier + tracking number, instead of the
        "Pickup" placeholder confirm_order() sends. This is what should drive
        real order completion for anything that isn't picked up in person."""
        data = request.get_json()
        order_num = data.get("order_num", "")
        carrier = (data.get("carrier") or "").upper()
        tracking_number = data.get("tracking_number", "")
        if carrier not in VALID_CARRIERS:
            return jsonify({"ok": False, "error": f"Invalid carrier — must be one of {', '.join(sorted(VALID_CARRIERS))}"})
        if not tracking_number and carrier != "PICKUP":
            return jsonify({"ok": False, "error": "Tracking number is required"})
        cfg = config.load()
        ok, err = pdx_api.shipped_callback(cfg.get("lab_id", ""), cfg.get("api_key", ""),
                                           order_num, carrier=carrier, tracking_number=tracking_number)
        if not ok:
            _log(f"Mark shipped failed for {order_num}: {err}", "error")
            return jsonify({"ok": False, "error": err})
        db.confirm_order(order_num)
        _log(f"📦 Shipped ({carrier} {tracking_number}): {order_num}")
        push_event("order_confirmed", {"order_num": order_num})
        return jsonify({"ok": True})

    @app.route("/api/fulfill_order", methods=["POST"])
    def fulfill_order():
        data = request.get_json()
        order_num = data.get("order_num", "")
        cfg = config.load()
        output_folder = cfg.get("image_output_folder", "")
        print_mode = cfg.get("print_mode", "auto")
        images = db.get_images_json(order_num)
        if not images:
            return jsonify({"ok": False, "error": "No images found"})

        if print_mode == "manual":
            # Download images to hot folder now so DNP picks them up
            if not output_folder:
                return jsonify({"ok": False, "error": "No hot folder configured"})
            api_key = cfg.get("api_key", "")
            db.set_download_status(order_num, "pending")
            ok, err = printer.download_images(images, output_folder, order_num=order_num, api_key=api_key)
            if not ok:
                db.set_download_status(order_num, "failed", err)
                _log(f"Manual print failed for {order_num}: {err}", "error")
                return jsonify({"ok": False, "error": err})
            db.set_download_status(order_num, "ok")
        else:
            # Auto mode: archive files that were already auto-downloaded
            ok, err = printer.fulfill_to_hot_folder(images, output_folder, order_num=order_num)
            if not ok:
                _log(f"Fulfill failed for {order_num}: {err}", "error")
                return jsonify({"ok": False, "error": err})

        db.set_fulfilled(order_num)
        _log(f"🖨 Sent to printer: {order_num}")
        push_event("order_fulfilled", {"order_num": order_num})
        return jsonify({"ok": True})

    # ── Packing slip (in-studio) ────────────────────────────────────────────────
    # Printing itself now happens client-side: the browser opens the PDF this
    # builds and shows its own print dialog, so staff can pick/confirm a printer.
    # These endpoints only build the document and record that it was handled.

    @app.route("/api/packing_slip_pdf", methods=["POST"])
    def packing_slip_pdf():
        data = request.get_json() or {}
        order_nums = data.get("order_nums", [])
        if not order_nums:
            return jsonify({"ok": False, "error": "No orders specified"}), 400

        orders = [db.get_order(n) for n in order_nums]
        orders = [o for o in orders if o]
        if not orders:
            return jsonify({"ok": False, "error": "Order(s) not found"}), 404

        cfg = config.load()
        destinations = db.get_destinations()
        pdf_bytes = printer.build_packing_slips_pdf(
            orders, destinations, cfg.get("studio_name", ""), cfg.get("image_output_folder", "")
        )
        return Response(pdf_bytes, mimetype="application/pdf")

    @app.route("/api/mark_slips_printed", methods=["POST"])
    def mark_slips_printed():
        data = request.get_json() or {}
        order_nums = data.get("order_nums", [])
        marked = []
        for order_num in order_nums:
            if db.set_fulfilled(order_num):
                marked.append(order_num)
        if marked:
            _log(f"🖨 Packing slip{'s' if len(marked) != 1 else ''} marked printed: {', '.join(marked)}")
            push_event("order_fulfilled", {"order_num": marked[0] if len(marked) == 1 else None, "batch": len(marked) > 1})
        return jsonify({"ok": True, "marked": marked})

    @app.route("/api/reprint_receipt", methods=["POST"])
    def reprint_receipt():
        data = request.get_json()
        order_num = data.get("order_num", "")
        cfg = config.load()
        order = db.get_order(order_num)
        if not order:
            return jsonify({"ok": False, "error": "Order not found"})
        try:
            ok, err = printer.print_receipt(
                order,
                cfg.get("printer_name", ""),
                cfg.get("studio_name", ""),
                cfg.get("logo_path", ""),
            )
            if ok:
                _log(f"🧾 Receipt reprinted: {order_num}")
            return jsonify({"ok": ok, "error": err})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/reprint_images", methods=["POST"])
    def reprint_images():
        data = request.get_json()
        order_num = data.get("order_num", "")
        cfg = config.load()
        output_folder = cfg.get("image_output_folder", "")
        api_key = cfg.get("api_key", "")
        selected_filenames = data.get("filenames")
        images = db.get_images_json(order_num)
        if not images:
            return jsonify({"ok": False, "error": "No images found"})
        if selected_filenames:
            images = [img for img in images if img.get("filename") in selected_filenames]
            if not images:
                return jsonify({"ok": False, "error": "Selected images not found in order"})
        reprint_filenames = [img.get("filename") for img in images if img.get("filename")]
        # Try archive restore first
        ok, err = printer.reprint_images_to_hot_folder(images, output_folder, order_num=order_num)
        if ok:
            db.reset_order_items_to_queued(order_num, reprint_filenames)
            _log(f"🔁 Reprint queued: {order_num}")
            return jsonify({"ok": True})
        # Fall back to re-downloading from API (files may have been consumed by DNP)
        _log(f"🔁 Archive not found, re-downloading {order_num}…")
        ok2, err2 = printer.download_images(images, output_folder, order_num=order_num, api_key=api_key)
        if ok2:
            db.reset_order_items_to_queued(order_num, reprint_filenames)
            _log(f"🔁 Reprint re-downloaded: {order_num}")
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": err2})

    @app.route("/api/retry_download", methods=["POST"])
    def retry_download():
        data = request.get_json()
        order_num = data.get("order_num", "")
        cfg = config.load()
        output_folder = cfg.get("image_output_folder", "")
        api_key = cfg.get("api_key", "")
        if not output_folder:
            return jsonify({"ok": False, "error": "No image output folder configured"})
        images = db.get_images_json(order_num)
        if not images:
            return jsonify({"ok": False, "error": "No image data found"})
        db.set_download_status(order_num, "pending")
        def _do():
            ok, err = printer.download_images(images, output_folder, order_num=order_num, api_key=api_key)
            db.set_download_status(order_num, "ok" if ok else "failed", err)
            push_event("download_done", {"order_num": order_num, "ok": ok, "error": err})
        threading.Thread(target=_do, daemon=True).start()
        return jsonify({"ok": True})

    # ── Image serving ─────────────────────────────────────────────────────────

    @app.route("/api/image/<order_num>/<path:filename>")
    def serve_image(order_num, filename):
        cfg = config.load()
        output_folder = cfg.get("image_output_folder", "")
        path = printer.get_image_path(filename, output_folder, order_num=order_num) if output_folder else None
        if path:
            mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
            return send_file(path, mimetype=mime)
        # Local file not available — redirect to CDN asset URL from DB
        images = db.get_images_json(order_num)
        img_data = next((i for i in images if i.get("filename") == filename), None)
        if img_data and img_data.get("assetUrl"):
            return redirect(img_data["assetUrl"])
        return "Image not found", 404

    # Fallback image route without order_num
    @app.route("/api/image/<path:filename>")
    def serve_image_flat(filename):
        cfg = config.load()
        output_folder = cfg.get("image_output_folder", "")
        path = printer.get_image_path(filename, output_folder) if output_folder else None
        if path:
            mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
            return send_file(path, mimetype=mime)
        return "Image not found", 404

    # ── Samples browser ───────────────────────────────────────────────────────

    @app.route("/api/samples/list")
    def samples_list():
        folder = request.args.get("folder", "")
        cfg = config.load()
        if not folder:
            folder = cfg.get("samples_folder", cfg.get("image_output_folder", ""))
        if not folder or not os.path.isdir(folder):
            return jsonify({"files": [], "folder": folder})
        files = []
        try:
            for fname in sorted(os.listdir(folder)):
                ext = os.path.splitext(fname)[1].lower()
                if ext in SUPPORTED_IMAGE_EXTS:
                    fpath = os.path.join(folder, fname)
                    files.append({
                        "filename": fname,
                        "size": os.path.getsize(fpath),
                        "url": f"/api/samples/image?folder={folder}&filename={fname}"
                    })
        except Exception as e:
            return jsonify({"files": [], "error": str(e)})
        return jsonify({"files": files, "folder": folder})

    @app.route("/api/samples/image")
    def samples_image():
        folder   = request.args.get("folder", "")
        filename = request.args.get("filename", "")
        if not folder or not filename:
            return "Missing params", 400
        path = os.path.join(folder, filename)
        if not os.path.exists(path):
            return "Not found", 404
        mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
        return send_file(path, mimetype=mime)

    @app.route("/api/samples/print", methods=["POST"])
    def samples_print():
        import shutil
        data = request.get_json()
        filenames  = data.get("filenames", [])
        src_folder = data.get("folder", "")
        cfg = config.load()
        hot_folder = cfg.get("image_output_folder", "")
        if not hot_folder:
            return jsonify({"ok": False, "error": "No hot folder configured"})
        errors = []
        for fname in filenames:
            try:
                shutil.copy2(os.path.join(src_folder, fname), os.path.join(hot_folder, fname))
                _log(f"🖼 Sample printed: {fname}")
            except Exception as e:
                errors.append(f"{fname}: {e}")
        if errors:
            return jsonify({"ok": False, "error": "; ".join(errors)})
        return jsonify({"ok": True, "count": len(filenames)})

    # ── Poller ────────────────────────────────────────────────────────────────

    @app.route("/api/get_poller_status")
    def get_poller_status():
        return jsonify(poller.get_status())

    @app.route("/api/trigger_poll", methods=["POST"])
    def trigger_poll():
        poller.trigger()
        return jsonify({"ok": True})

    # ── Activity log ──────────────────────────────────────────────────────────

    @app.route("/api/activity_log")
    def activity_log():
        limit = int(request.args.get("limit", 50))
        return jsonify(db.get_activity_log(limit))

    @app.route("/api/activity_log_write", methods=["POST"])
    def activity_log_write():
        data = request.get_json()
        msg   = data.get("message", "")
        level = data.get("level", "info")
        if msg:
            _log(msg, level)
        return jsonify({"ok": True})

    # ── OTA Updates ───────────────────────────────────────────────────────────

    @app.route("/api/check_update")
    def check_update():
        import updater
        info = updater.check_for_update()
        if info:
            return jsonify({"update_available": True, **info})
        return jsonify({"update_available": False, "current": updater.APP_VERSION})

    @app.route("/api/install_update", methods=["POST"])
    def install_update():
        import updater
        data = request.get_json()
        download_url = data.get("download_url", "")
        if not download_url:
            return jsonify({"ok": False, "error": "No download URL"})
        def on_progress(pct, msg):
            push_event("update_progress", {"pct": pct, "message": msg})
        def on_complete():
            push_event("update_progress", {"pct": 100, "message": "Restarting…"})
        def on_error(err):
            push_event("update_error", {"error": err})
            _log(f"Update failed: {err}", "error")
        _log("🔄 Starting update download…")
        updater.download_and_install(download_url, on_progress=on_progress, on_complete=on_complete, on_error=on_error)
        return jsonify({"ok": True})

    @app.route("/api/get_version")
    def get_version():
        import updater
        return jsonify({"version": updater.APP_VERSION})

    @app.route("/api/get_pending_update")
    def get_pending_update():
        """UI polls this on load to check if an update was found before SSE connected."""
        if _pending_update:
            return jsonify({"update_available": True, **_pending_update})
        return jsonify({"update_available": False})

    return app


def _seed_jobs_background(lab_id: str, api_key: str):
    """Fetch historical orders to build jobs list and run status migration."""
    import api as pdx_api, db
    try:
        # Run status migration first — fix any orders with wrong status in DB
        db.migrate_fulfilled_orders(lab_id, api_key)

        # Seed jobs from existing DB orders
        galleries = db.get_all_galleries()
        for gallery in galleries:
            db.upsert_job(gallery)

        # Fetch historical orders from PDX API to catch jobs not yet in local DB
        orders, err = pdx_api.fetch_historical_orders(lab_id, api_key, limit_per_status=100)
        if not err:
            for order_data in orders:
                # Upsert order into DB (handles status mapping)
                db.upsert_order(order_data)
                gallery = order_data.get("gallery", "")
                if gallery:
                    db.upsert_job(gallery)
            push_event("jobs_updated", {"count": len(db.get_jobs())})
            log.info(f"[Jobs] Seeded {len(db.get_jobs())} jobs")
    except Exception as e:
        log.warning(f"[Jobs] Seed failed: {e}")

"""
server.py — Flask app for PDX Onsite
"""
import base64
import io
import json
import logging
import mimetypes
import os
import queue
import socket
import sys
import threading
import tkinter as tk
import zipfile
from datetime import datetime
from tkinter import filedialog
from typing import Optional

import requests
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, send_file

import api as pdx_api
import config
import db
import discovery
import printer
import shipping_providers

log = logging.getLogger("pdx.server")
# One queue per connected browser (not a single shared queue) — with two
# stations sharing this backend over LAN (see get_lan_ip below), each needs
# its own copy of every event. A shared queue would round-robin events
# across clients instead of broadcasting, so one station would silently miss
# updates the other happened to dequeue first.
_sse_clients: list = []
_sse_clients_lock = threading.Lock()
# Serializes mark_shipped / mark_ready_to_ship end to end (check → external
# calls → record). Without this, two near-simultaneous requests for the same
# order (now a real possibility with multi-station sharing one backend, or
# just a double-click) both pass has_shipped_notification() before either
# writes it, and each buys a real shipping label. A single coarse lock is
# fine here — this is a staff button click, not a hot path.
_shipping_action_lock = threading.Lock()
SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
MAX_LOGO_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB
_pending_update: Optional[dict] = None


def set_pending_update(info: dict):
    global _pending_update
    _pending_update = info


def get_lan_ip() -> Optional[str]:
    """Best-effort LAN address for this machine, shown to staff so a second
    station can point a browser at it. Doesn't actually send any traffic —
    opening a UDP socket to a public IP just makes the OS pick the outbound
    interface, which is all we need the address of.

    Returns None on failure (e.g. no default route/gateway — an isolated
    event LAN, or outbound UDP blocked) — NOT "127.0.0.1". A loopback
    fallback used to be returned here, which is actively worse than no
    answer: a second station would silently store its own loopback address
    as "the primary" and never reach it, with nothing to explain why."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def get_lan_url() -> Optional[str]:
    """This station's own address, or None if it couldn't be determined —
    see get_lan_ip. Centralizes the URL string (port 5050) instead of every
    call site rebuilding it by hand."""
    ip = get_lan_ip()
    return f"http://{ip}:5050" if ip else None


def push_event(event: str, data: dict):
    try:
        payload = json.dumps({"event": event, "data": data})
        msg = f"data: {payload}\n\n"
    except Exception:
        return
    with _sse_clients_lock:
        clients = list(_sse_clients)
    for client_queue in clients:
        try:
            client_queue.put_nowait(msg)
        except queue.Full:
            pass


def _log(msg: str, level: str = "info"):
    db.log_activity(msg, level)
    push_event("activity", {"message": msg, "level": level})
    getattr(log, level, log.info)(msg)


def _station_tag() -> str:
    """A secondary station tags its requests with X-Station-Name (see
    app.js apiPost) so the Activity Log can show which physical station did
    what — the whole reason this exists is being able to answer exactly
    that question after an event."""
    name = request.headers.get("X-Station-Name", "").strip()
    return f" ({name})" if name else ""


def create_app(poller, ui_path: str = "") -> Flask:
    app = Flask(__name__, static_folder=None)

    # Seed default destination from config on startup (no-op if destinations exist)
    cfg = config.load()
    if cfg.get("image_output_folder"):
        db.seed_default_destination(cfg["image_output_folder"])

    # ── Multi-station (same-location, onsite-only workflow) ─────────────────
    discovery_responder = discovery.DiscoveryResponder(get_info=lambda: {
        "name": config.load().get("station_name", ""),
        "url": get_lan_url(),
        "studio_name": config.load().get("studio_name", ""),
    })
    if cfg.get("station_role") == "primary":
        discovery_responder.start()

    def _start_as_primary(name: str):
        config.save({"station_role": "primary", "station_name": name, "joined_primary_url": ""})
        c = config.load()
        if c.get("lab_id") and c.get("api_key"):
            poller.configure(c["lab_id"], c["api_key"], int(c.get("poll_interval", 60)))
            if not poller.running:
                poller.start()
        discovery_responder.start()

    def _stop_being_primary():
        if poller.running:
            poller.stop()
        discovery_responder.stop()

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
        client_queue: queue.Queue = queue.Queue(maxsize=200)
        with _sse_clients_lock:
            _sse_clients.append(client_queue)

        def generate():
            try:
                yield "data: {\"event\":\"connected\"}\n\n"
                while True:
                    try:
                        msg = client_queue.get(timeout=15)
                        yield msg
                    except queue.Empty:
                        yield ": heartbeat\n\n"
            finally:
                # Runs when the browser tab closes / navigates away and the
                # generator is torn down — keeps _sse_clients from growing
                # forever as stations connect and disconnect over a long event.
                with _sse_clients_lock:
                    if client_queue in _sse_clients:
                        _sse_clients.remove(client_queue)
        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.route("/api/station/info")
    def station_info():
        c = config.load()
        return jsonify({
            "role": c.get("station_role", "solo"),
            "name": c.get("station_name", ""),
            "joined_primary_url": c.get("joined_primary_url", ""),
            "lan_url": get_lan_url(),  # None if it couldn't be determined — see get_lan_ip
            "studio_name": c.get("studio_name", ""),
        })

    @app.route("/api/station/discover")
    def station_discover():
        my_url = get_lan_url()
        stations = [s for s in discovery.discover_stations() if s["url"] != my_url]
        return jsonify({"stations": stations})

    @app.route("/api/station/become_primary", methods=["POST"])
    def become_primary():
        """The one action that covers three cases: a fresh station going
        primary for the first time, an emergency promotion because the real
        primary is unreachable, and gracefully reclaiming the role from a
        primary that's actually still healthy (including the original
        primary coming back and taking its role back). Whenever another
        primary answers on the network, a coordinated handoff is attempted
        first so there's never a window where two stations both believe
        they're primary — only if that station is genuinely unreachable does
        this fall back to the soft-warned emergency path."""
        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "Station name is required"})

        my_url = get_lan_url()
        if not my_url:
            # Becoming primary with no determinable address defeats the
            # whole point — no other station could ever find or connect to
            # this one, and it would silently announce a broken address to
            # anyone who tries (see discovery.py's matching guard).
            return jsonify({"ok": False, "error":
                           "Could not determine this computer's network address — check its WiFi/network connection and try again."})
        others = [s for s in discovery.discover_stations() if s.get("name") and s["url"] != my_url]

        if others:
            target = others[0]
            try:
                resp = requests.post(f"{target['url']}/api/station/step_down",
                                     json={"new_primary_url": my_url, "new_primary_name": name}, timeout=4)
                result = resp.json()
                if not result.get("ok"):
                    return jsonify({"ok": False, "error": result.get("error", "The current primary refused to step down")})
                others = []  # confirmed clear — it just stood down for us
            except Exception:
                # Unreachable despite just answering the discovery broadcast
                # (a tight race) — proceed like the emergency-promotion path,
                # with the same soft warning below.
                pass

        _start_as_primary(name)
        _log(f"🔷 This station is now the main station: \"{name}\"")

        if others:
            names = ", ".join(f'"{o["name"]}"' for o in others)
            _log(f"⚠ Another primary station ({names}) was already active on this network "
                 f"when \"{name}\" was set up as primary — this can cause duplicate prints.", "error")
            return jsonify({"ok": True, "other_primary_found": others[0]})
        return jsonify({"ok": True, "other_primary_found": None})

    @app.route("/api/station/join", methods=["POST"])
    def station_join():
        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        primary_url = (data.get("primary_url") or "").strip()
        primary_name = (data.get("primary_name") or "").strip()
        if not name or not primary_url:
            return jsonify({"ok": False, "error": "Station name and a station to join are required"})
        _stop_being_primary()
        config.save({"station_role": "secondary", "station_name": name, "joined_primary_url": primary_url})
        _log(f"🔗 This station joined \"{primary_name or primary_url}\" as \"{name}\"")
        return jsonify({"ok": True})

    @app.route("/api/station/step_down", methods=["POST"])
    def station_step_down():
        """Called BY another station reclaiming the primary role — not
        something a person clicks directly. Stops acting as primary
        immediately and tells any locally-connected browser to redirect."""
        data = request.get_json() or {}
        new_primary_url = (data.get("new_primary_url") or "").strip()
        new_primary_name = (data.get("new_primary_name") or "").strip()
        c = config.load()
        if c.get("station_role") != "primary":
            return jsonify({"ok": False, "error": "This station is not currently the primary"})
        if not new_primary_url:
            return jsonify({"ok": False, "error": "new_primary_url is required"})

        my_name = c.get("station_name", "")
        _stop_being_primary()
        config.save({"station_role": "secondary", "station_name": my_name, "joined_primary_url": new_primary_url})
        _log(f"⬇ Stepped down as main station — \"{new_primary_name or new_primary_url}\" is now primary")
        push_event("station_demoted", {"redirect_url": f"{new_primary_url}?station={my_name}"})
        return jsonify({"ok": True})

    @app.route("/api/station/reset_to_solo", methods=["POST"])
    def station_reset_to_solo():
        _stop_being_primary()
        config.save({"station_role": "solo", "station_name": "", "joined_primary_url": ""})
        _log("Station reset to standalone (solo) mode")
        return jsonify({"ok": True})

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
        env = data.get("api_environment") or "production"
        ok, msg = pdx_api.test_connection(data.get("lab_id", ""), data.get("api_key", ""),
                                          environment=env)
        _log(f"Test connection ({env}): {'✓' if ok else '✗'} {msg}", "info" if ok else "warning")
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

    # ── Shipping providers ───────────────────────────────────────────────────────
    # Onsite creates the provider order at ingestion (poller.py) and creates the
    # label on demand when staff click "Ready to Ship" (below) — no discovery
    # polling, this app pushes data in rather than discovering it after the fact.

    @app.route("/api/get_shipping_provider_catalog")
    def get_shipping_provider_catalog():
        """Every provider type this app knows how to talk to, and the
        credential fields each one needs — lets the Settings UI render an
        "add provider" form generically instead of hardcoding one shape."""
        return jsonify(shipping_providers.provider_catalog())

    @app.route("/api/get_shipping_providers")
    def get_shipping_providers():
        return jsonify(db.get_shipping_providers())

    @app.route("/api/save_shipping_provider", methods=["POST"])
    def save_shipping_provider():
        data = request.get_json() or {}
        provider_type = data.get("provider_type", "")
        if provider_type not in shipping_providers.PROVIDER_ADAPTERS:
            return jsonify({"ok": False, "error": f"Unknown provider type: {provider_type}"})
        try:
            provider_id = db.upsert_shipping_provider(
                provider_type=provider_type,
                label=data.get("label", "").strip() or shipping_providers.PROVIDER_ADAPTERS[provider_type].display_name,
                credentials=data.get("credentials", {}),
                enabled=bool(data.get("enabled", True)),
                provider_id=data.get("id"),
            )
            return jsonify({"ok": True, "id": provider_id})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/delete_shipping_provider", methods=["POST"])
    def delete_shipping_provider():
        data = request.get_json() or {}
        provider_id = data.get("id")
        if not provider_id:
            return jsonify({"ok": False, "error": "id is required"})
        db.delete_shipping_provider(provider_id)
        return jsonify({"ok": True})

    @app.route("/api/list_provider_carriers")
    def list_provider_carriers():
        provider = db.get_shipping_provider(request.args.get("provider_id", type=int))
        if not provider:
            return jsonify({"ok": False, "error": "Provider not found"})
        try:
            adapter = shipping_providers.get_adapter(provider["provider_type"], provider["credentials"])
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)})
        carriers, err = adapter.list_carriers()
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, "carriers": carriers})

    @app.route("/api/list_provider_services")
    def list_provider_services():
        provider = db.get_shipping_provider(request.args.get("provider_id", type=int))
        carrier_code = request.args.get("carrier_code", "")
        if not provider or not carrier_code:
            return jsonify({"ok": False, "error": "provider_id and carrier_code are required"})
        try:
            adapter = shipping_providers.get_adapter(provider["provider_type"], provider["credentials"])
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)})
        services, err = adapter.list_services(carrier_code)
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, "services": services})

    @app.route("/api/list_provider_packages")
    def list_provider_packages():
        provider = db.get_shipping_provider(request.args.get("provider_id", type=int))
        carrier_code = request.args.get("carrier_code", "")
        if not provider or not carrier_code:
            return jsonify({"ok": False, "error": "provider_id and carrier_code are required"})
        try:
            adapter = shipping_providers.get_adapter(provider["provider_type"], provider["credentials"])
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)})
        packages, err = adapter.list_packages(carrier_code)
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, "packages": packages})

    @app.route("/api/get_known_shipping_options")
    def get_known_shipping_options():
        return jsonify(db.get_known_pdx_shipping_options())

    @app.route("/api/get_shipping_option_mappings")
    def get_shipping_option_mappings():
        provider_id = request.args.get("provider_id", type=int)
        if not provider_id:
            return jsonify([])
        return jsonify(db.get_shipping_option_mappings(provider_id))

    @app.route("/api/save_shipping_option_mapping", methods=["POST"])
    def save_shipping_option_mapping():
        data = request.get_json() or {}
        provider_id = data.get("provider_id")
        option_id = data.get("pdx_option_external_id", "")
        if not provider_id or not option_id:
            return jsonify({"ok": False, "error": "provider_id and pdx_option_external_id are required"})
        db.upsert_shipping_option_mapping(
            provider_id=provider_id,
            pdx_option_external_id=option_id,
            pdx_option_name=data.get("pdx_option_name", ""),
            carrier_code=data.get("carrier_code", ""),
            service_code=data.get("service_code", ""),
            package_code=data.get("package_code", ""),
            confirmation=data.get("confirmation", "none"),
            pdx_carrier=data.get("pdx_carrier", ""),
        )
        return jsonify({"ok": True})

    @app.route("/api/mark_ready_to_ship", methods=["POST"])
    def mark_ready_to_ship():
        """The automated counterpart to Mark Shipped — staff confirm the order
        is actually printed and packed, Onsite creates the shipping label
        itself (using the mapping configured for this order's PDX shipping
        option) and reports the resulting tracking number to PDX. Exactly the
        same safety rule as everywhere else: PDX must confirm success before
        anything changes locally."""
        data = request.get_json() or {}
        order_num = data.get("order_num", "")
        cfg = config.load()
        try:
            weight_lb = float(data.get("weight_lb", cfg.get("default_package_weight_lb", 0.1)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid weight"})
        if weight_lb <= 0:
            return jsonify({"ok": False, "error": "Weight must be greater than 0"})
        # Locked end-to-end: without this, two near-simultaneous requests for
        # the same order (multi-station, or a double-click) could both pass
        # the has_shipped_notification check below before either records it,
        # and each buy a real shipping label.
        with _shipping_action_lock:
            order = db.get_order(order_num)
            if not order:
                return jsonify({"ok": False, "error": "Order not found"})
            if db.has_shipped_notification(order_num):
                return jsonify({"ok": False, "error": "Order already marked shipped"})

            provider = (db.get_shipping_provider(order["ship_provider_id"])
                       if order.get("ship_provider_id") else db.get_enabled_shipping_provider())
            if not provider:
                return jsonify({"ok": False, "error": "No shipping provider configured"})

            try:
                raw = json.loads(order.get("raw_json") or "{}")
            except Exception:
                raw = {}
            shipping = raw.get("shipping") or {}
            option_external_id = (shipping.get("option") or {}).get("externalId", "")
            mapping = db.get_shipping_option_mapping(provider["id"], option_external_id)
            if not mapping or not mapping.get("carrier_code"):
                return jsonify({"ok": False, "error":
                               f"No shipping mapping configured for option \"{option_external_id}\" — "
                               f"set one up in Settings, or use Mark Shipped instead"})
            if not mapping.get("pdx_carrier"):
                # A real label must never get purchased for a mapping that can't
                # actually report back to PDX — carrier_code alone (ShipStation's
                # side) isn't enough; pdx_carrier (what gets sent to PDX) can be
                # left "— Do not map —" in Settings, which saves it as null.
                return jsonify({"ok": False, "error":
                               f"Shipping option \"{option_external_id}\" is mapped to a carrier but not a PDX Carrier — "
                               f"finish that mapping in Settings, or use Mark Shipped instead"})

            try:
                adapter = shipping_providers.get_adapter(provider["provider_type"], provider["credentials"])
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)})

            external_order_id = order.get("ship_external_order_id")
            if not external_order_id:
                # Order-creation at ingestion didn't happen (provider added after
                # this order arrived, or it failed at the time) — create it now.
                external_order_id, err = adapter.create_order({
                    "order_num": order_num,
                    "placed_at": order.get("placed_at", ""),
                    "studio_name": cfg.get("studio_name", ""),
                    "destination": shipping.get("destination", {}),
                })
                if err:
                    return jsonify({"ok": False, "error": f"Could not create provider order: {err}"})
                db.set_order_ship_provider(order_num, provider["id"], external_order_id)

            ship_date = datetime.now().strftime("%Y-%m-%d")
            result, err = adapter.create_label(
                external_order_id, mapping["carrier_code"], mapping["service_code"],
                mapping.get("package_code", ""), mapping.get("confirmation", "none"), ship_date,
                weight_lb,
            )
            if err:
                _log(f"Ready to Ship failed for {order_num}: {err}", "error")
                return jsonify({"ok": False, "error": err})

            tracking_number = result.get("tracking_number", "")
            ok, pdx_err = pdx_api.shipped_callback(cfg.get("lab_id", ""), cfg.get("api_key", ""), order_num,
                                                   carrier=mapping["pdx_carrier"], tracking_number=tracking_number)
            if ok or pdx_api.is_already_shipped_error(pdx_err):
                db.confirm_order(order_num)
                db.record_shipped_notification(order_num, mapping["pdx_carrier"], tracking_number, provider["provider_type"])
                if result.get("label_data"):
                    db.save_order_ship_label(order_num, result["label_data"])
                push_event("order_confirmed", {"order_num": order_num})
                _log(f"📦 Ready to ship: {order_num} ({mapping['pdx_carrier']} {tracking_number})")
                return jsonify({"ok": True, "tracking_number": tracking_number, "carrier": mapping["pdx_carrier"]})
            _log(f"Ready to Ship: label created for {order_num} but PDX rejected it — {pdx_err}", "error")
            return jsonify({"ok": False, "error": f"Label created (tracking {tracking_number}) but PDX call failed: {pdx_err}"})

    @app.route("/api/get_shipping_label")
    def get_shipping_label():
        """Re-serves the real carrier label already purchased for this order —
        for reprinting, never for buying a new one (that only ever happens in
        mark_ready_to_ship)."""
        order_num = request.args.get("order_num", "")
        order = db.get_order(order_num)
        if not order or not order.get("ship_label_data"):
            return jsonify({"error": "No label on file for this order"}), 404
        pdf_bytes = base64.b64decode(order["ship_label_data"])
        return Response(pdf_bytes, mimetype="application/pdf")

    @app.route("/api/test_ready_to_ship", methods=["POST"])
    def test_ready_to_ship():
        """Pure dry-run for verifying a shipping-provider mapping actually works:
        buys a real-but-void ShipStation label (testLabel=True, never charged)
        using the same carrier/service/package/weight the real Ready to Ship
        would use, but against a throwaway provider order — never touches the
        real order's ship_provider_id/ship_external_order_id, never calls PDX,
        never changes local order status. Safe to run against a real order."""
        data = request.get_json() or {}
        order_num = data.get("order_num", "")
        cfg = config.load()
        try:
            weight_lb = float(data.get("weight_lb", cfg.get("default_package_weight_lb", 0.1)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid weight"})
        if weight_lb <= 0:
            return jsonify({"ok": False, "error": "Weight must be greater than 0"})

        order = db.get_order(order_num)
        if not order:
            return jsonify({"ok": False, "error": "Order not found"})

        provider = (db.get_shipping_provider(order["ship_provider_id"])
                   if order.get("ship_provider_id") else db.get_enabled_shipping_provider())
        if not provider:
            return jsonify({"ok": False, "error": "No shipping provider configured"})

        try:
            raw = json.loads(order.get("raw_json") or "{}")
        except Exception:
            raw = {}
        shipping = raw.get("shipping") or {}
        option_external_id = (shipping.get("option") or {}).get("externalId", "")
        mapping = db.get_shipping_option_mapping(provider["id"], option_external_id)
        if not mapping or not mapping.get("carrier_code"):
            return jsonify({"ok": False, "error":
                           f"No shipping mapping configured for option \"{option_external_id}\" — set one up in Settings"})

        try:
            adapter = shipping_providers.get_adapter(provider["provider_type"], provider["credentials"])
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)})

        # A fresh, disposable provider order — deliberately never touches this
        # order's real ship_provider_id/ship_external_order_id.
        test_external_order_id, err = adapter.create_order({
            "order_num": f"{order_num}-TEST",
            "placed_at": order.get("placed_at", ""),
            "studio_name": cfg.get("studio_name", ""),
            "destination": shipping.get("destination", {}),
        })
        if err:
            return jsonify({"ok": False, "error": f"Could not create test provider order: {err}"})

        ship_date = datetime.now().strftime("%Y-%m-%d")
        result, err = adapter.create_label(
            test_external_order_id, mapping["carrier_code"], mapping["service_code"],
            mapping.get("package_code", ""), mapping.get("confirmation", "none"), ship_date,
            weight_lb, test_label=True,
        )
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({
            "ok": True,
            "tracking_number": result.get("tracking_number", ""),
            "label_data": result.get("label_data", ""),
        })

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
        _log(f"✅ Confirmed (scanned): {order_num}{_station_tag()}")
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
        # Same lock + dedup log Ready to Ship uses — without both, a station
        # that already shipped this order via the automated flow (or another
        # station, if multi-station is set up) could re-report it here too.
        with _shipping_action_lock:
            if db.has_shipped_notification(order_num):
                return jsonify({"ok": False, "error": "Order already marked shipped"})
            cfg = config.load()
            ok, err = pdx_api.shipped_callback(cfg.get("lab_id", ""), cfg.get("api_key", ""),
                                               order_num, carrier=carrier, tracking_number=tracking_number)
            if not ok:
                _log(f"Mark shipped failed for {order_num}: {err}", "error")
                return jsonify({"ok": False, "error": err})
            db.confirm_order(order_num)
            db.record_shipped_notification(order_num, carrier, tracking_number, "manual")
            _log(f"📦 Shipped ({carrier} {tracking_number}): {order_num}{_station_tag()}")
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

    @app.route("/api/export_logs")
    def export_logs():
        """One-click bundle for non-technical staff to send us after an
        incident: the full curated Activity Log (readable, unbounded — the
        live panel only ever shows the last 50) plus the raw technical
        pdx_onsite.log, zipped together. Nothing to hunt for on disk."""
        app_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
            else os.path.dirname(os.path.abspath(__file__))
        raw_log_path = os.path.join(app_dir, "pdx_onsite.log")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            lines = [f"{e['ts']}  [{e['level'].upper()}]  {e['message']}" for e in db.get_activity_log_all()]
            zf.writestr("activity_log.txt", "\n".join(lines) if lines else "(empty)")
            if os.path.exists(raw_log_path):
                zf.write(raw_log_path, "pdx_onsite.log")
            else:
                zf.writestr("pdx_onsite.log", "(not found on this machine)")
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True,
                         download_name=f"pdx_onsite_logs_{stamp}.zip")

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

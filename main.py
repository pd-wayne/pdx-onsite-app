"""
main.py — PDX Onsite Pickup Station v2.0
"""
import os
import sys
import logging
import threading
import webbrowser
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(
                os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)),
                "pdx_onsite.log"
            ),
            encoding="utf-8"
        )
    ]
)
log = logging.getLogger("pdx.main")

import db
import config
import poller as poller_module
import updater
from server import create_app

PORT = 5050


def get_ui_path() -> str:
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "src")


def open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{PORT}")


def _seed_jobs(lab_id: str, api_key: str):
    time.sleep(1)
    from server import _seed_jobs_background
    _seed_jobs_background(lab_id, api_key)


def _check_updates():
    time.sleep(2)
    def on_update_found(info):
        log.info(f"[Updater] Update available: v{info['latest']}")
        from server import push_event, set_pending_update
        set_pending_update(info)
        push_event("update_available", info)
        db.log_activity(f"Update available: v{info['current']} → v{info['latest']}")
    updater.check_async(on_update_available=on_update_found)


def main():
    log.info(f"=== PDX Onsite v{updater.APP_VERSION} starting ===")

    db.init_db()
    log.info("Database initialized")

    cfg = config.load()
    log.info(f"Config loaded — lab_id={cfg.get('lab_id', '(none)')}")

    p = poller_module.Poller()
    p.configure(
        cfg.get("lab_id", ""),
        cfg.get("api_key", ""),
        int(cfg.get("poll_interval", 60))
    )

    ui_path = get_ui_path()
    app = create_app(poller=p, ui_path=ui_path)

    if cfg.get("lab_id") and cfg.get("api_key"):
        p.start()
        log.info("Poller started")
        threading.Thread(
            target=_seed_jobs,
            args=(cfg["lab_id"], cfg["api_key"]),
            daemon=True
        ).start()
    else:
        log.info("No credentials — poller not started")

    threading.Thread(target=_check_updates, daemon=True).start()
    threading.Thread(target=open_browser, daemon=True).start()

    log.info(f"Starting Flask on http://localhost:{PORT}")
    try:
        app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
    finally:
        p.stop()
        log.info("=== PDX Onsite shutting down ===")


if __name__ == "__main__":
    main()

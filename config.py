"""
config.py — Settings management for PDX Onsite
"""
import json
import os
import sys

def _app_dir() -> str:
    """Return the directory where persistent data files should live.
    When running as a PyInstaller .exe, use the folder containing the exe.
    When running as a script, use the folder containing this file."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(_app_dir(), "pdx_onsite_config.json")

DEFAULTS = {
    "lab_id": "",
    "api_key": "",
    "printer_name": "",
    "studio_name": "",
    "poll_interval": 60,
    "unclaimed_threshold": 30,
    "logo_path": "",
    "image_output_folder": "",
    "samples_folder": "",
    "print_mode": "auto",           # "auto" | "manual"
    "destination_health_threshold": 10,  # minutes before a destination is flagged stale
    "api_environment": "production",  # "production" | "staging" — see api.py get_base_url()
    # Each environment keeps its own saved credentials — lab_id/api_key above
    # are always "whichever environment is currently active" (what the
    # poller, job-seeding, etc. actually use), kept in sync with these on
    # every save so nothing else in the app needs to change.
    "production_lab_id": "",
    "production_api_key": "",
    "staging_lab_id": "",
    "staging_api_key": "",
}


def load() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                saved = json.load(f)
            cfg = {**DEFAULTS, **saved}
        except Exception:
            cfg = dict(DEFAULTS)
    else:
        cfg = dict(DEFAULTS)

    # Migration: before per-environment credentials existed, lab_id/api_key
    # WERE the (implicitly production) credentials. Treat them as such
    # rather than a studio appearing to have lost their Lab ID the first
    # time they open Settings after this feature shipped.
    if not cfg.get("production_lab_id") and cfg.get("lab_id"):
        cfg["production_lab_id"] = cfg["lab_id"]
        cfg["production_api_key"] = cfg["api_key"]

    return cfg


def save(data: dict) -> bool:
    try:
        cfg = {**DEFAULTS, **data}
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception as e:
        print(f"[Config] Save failed: {e}")
        return False

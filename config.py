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
    "station_role": "solo",         # "solo" | "primary" | "secondary" — see discovery.py
    "station_name": "",
    "joined_primary_url": "",       # secondary only — last-known address of the primary it joined
    "api_environment": "production",  # "production" | "staging" — see api.py get_base_url()
}


def load() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                saved = json.load(f)
            return {**DEFAULTS, **saved}
        except Exception:
            pass
    return dict(DEFAULTS)


def save(data: dict) -> bool:
    """Merges `data` onto the CURRENTLY SAVED config, not onto bare DEFAULTS —
    a partial dict (anything not touching every key) must never silently
    reset the fields it didn't mention. This used to merge onto DEFAULTS
    instead, on the assumption every caller always submits the full settings
    object; the multi-station feature broke that assumption (a plain
    Settings-page Save doesn't know about station_role/station_name/
    joined_primary_url, so it was wiping them back to "solo" every time) —
    found by code review, not by a caller actually hitting it in the wild."""
    try:
        cfg = {**load(), **data}
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception as e:
        print(f"[Config] Save failed: {e}")
        return False

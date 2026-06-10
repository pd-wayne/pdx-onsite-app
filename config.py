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
    "fulfillment_mode": "pickup",   # "pickup" | "dropship" | "both"
    "print_mode": "auto",           # "auto" | "manual"
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
    try:
        cfg = {**DEFAULTS, **data}
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception as e:
        print(f"[Config] Save failed: {e}")
        return False

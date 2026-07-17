"""
updater.py — OTA update checker for PDX Onsite

Checks GitHub for a newer version on startup.
If found, shows a notification in the UI — user triggers the update.
Update downloads the new .exe in the background, writes a swap batch file,
launches the batch file detached, then exits so Windows can replace the exe.
"""
import logging
import os
import sys
import threading
import time
from typing import Optional

import requests

log = logging.getLogger("pdx.updater")

APP_VERSION = "2.0.1"
VERSION_URL = "https://raw.githubusercontent.com/pd-wayne/pdx-onsite-app/main/version.json"
CHECK_TIMEOUT = 8
EXE_NAME = "PDX_Onsite.exe"

PRESERVE = {
    "pdx_onsite.db",
    "pdx_onsite_config.json",
    "pdx_onsite.log",
}


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def parse_version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.strip().lower().lstrip("v").split("."))
    except Exception:
        return (0, 0, 0)


def check_for_update() -> Optional[dict]:
    try:
        resp = requests.get(VERSION_URL, timeout=CHECK_TIMEOUT)
        if not resp.ok:
            log.warning(f"[Updater] Version check failed: HTTP {resp.status_code}")
            return None
        data = resp.json()
        remote_version = data.get("version", "0.0.0")
        if parse_version(remote_version) > parse_version(APP_VERSION):
            log.info(f"[Updater] Update available: {APP_VERSION} → {remote_version}")
            return {
                "current": APP_VERSION,
                "latest": remote_version,
                "download_url": data.get("download_url", ""),
                "release_notes": data.get("release_notes", ""),
            }
        log.info(f"[Updater] Up to date: v{APP_VERSION}")
        return None
    except requests.exceptions.ConnectionError:
        log.info("[Updater] No internet — skipping update check")
        return None
    except requests.exceptions.Timeout:
        log.info("[Updater] Update check timed out")
        return None
    except Exception as e:
        log.warning(f"[Updater] Update check error: {e}")
        return None


def download_and_install(download_url: str, on_progress=None, on_complete=None, on_error=None):
    def _run():
        app_dir = _app_dir()
        update_exe = os.path.join(app_dir, "PDX_Onsite_update.exe")
        current_exe = sys.executable if getattr(sys, "frozen", False) else os.path.join(app_dir, EXE_NAME)
        bat_path = os.path.join(app_dir, "_pdx_update.bat")

        try:
            log.info(f"[Updater] Downloading from {download_url}")
            if on_progress:
                on_progress(0, "Downloading update…")

            resp = requests.get(download_url, stream=True, timeout=180)
            if not resp.ok:
                raise Exception(f"Download failed: HTTP {resp.status_code}")

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            with open(update_exe, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total and on_progress:
                        pct = int(downloaded / total * 90)
                        on_progress(pct, f"Downloading… {pct}%")

            if on_progress:
                on_progress(95, "Preparing update…")

            # Batch file: waits for app to exit, swaps exe, relaunches, deletes itself
            bat = (
                "@echo off\n"
                "ping -n 4 127.0.0.1 > nul\n"
                f"move /Y \"{update_exe}\" \"{current_exe}\"\n"
                f"start \"\" \"{current_exe}\"\n"
                "del \"%~f0\"\n"
            )
            with open(bat_path, "w") as f:
                f.write(bat)

            if on_progress:
                on_progress(100, "Restarting…")

            log.info("[Updater] Launching swap script and exiting")

            if on_complete:
                on_complete()

            time.sleep(1.5)

            import subprocess
            subprocess.Popen(
                ["cmd.exe", "/c", bat_path],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            os._exit(0)

        except Exception as e:
            log.error(f"[Updater] Update failed: {e}")
            for path in (update_exe, bat_path):
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except Exception:
                    pass
            if on_error:
                on_error(str(e))

    threading.Thread(target=_run, daemon=True).start()


def check_async(on_update_available=None):
    def _run():
        info = check_for_update()
        if info and on_update_available:
            on_update_available(info)

    threading.Thread(target=_run, daemon=True).start()

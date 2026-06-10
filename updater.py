"""
updater.py — OTA update checker for PDX Onsite
Checks GitHub for a newer version on startup.
If found, shows a notification in the UI — user triggers the update.
Update downloads in background, extracts over existing files, restarts app.
"""
import logging
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from typing import Optional

import requests

log = logging.getLogger("pdx.updater")

APP_VERSION = "2.0.0"
VERSION_URL = "https://raw.githubusercontent.com/pd-wayne/pdx-onsite-app/main/version.json"
CHECK_TIMEOUT = 8

PRESERVE = {
    "pdx_onsite.db",
    "pdx_onsite_config.json",
    "pdx_onsite.log",
}


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
        try:
            log.info(f"[Updater] Downloading from {download_url}")
            if on_progress:
                on_progress(0, "Downloading update…")

            resp = requests.get(download_url, stream=True, timeout=120)
            if not resp.ok:
                raise Exception(f"Download failed: HTTP {resp.status_code}")

            total = int(resp.headers.get("content-length", 0))
            downloaded = 0

            tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False, prefix="pdx_update_")
            with open(tmp_zip.name, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total and on_progress:
                        pct = int(downloaded / total * 70)
                        on_progress(pct, f"Downloading… {pct}%")

            if on_progress:
                on_progress(70, "Extracting…")

            app_dir = os.path.dirname(os.path.abspath(__file__))
            tmp_extract = tempfile.mkdtemp(prefix="pdx_update_extract_")

            with zipfile.ZipFile(tmp_zip.name, "r") as zf:
                zf.extractall(tmp_extract)

            extracted_items = os.listdir(tmp_extract)
            if len(extracted_items) == 1 and os.path.isdir(os.path.join(tmp_extract, extracted_items[0])):
                src_dir = os.path.join(tmp_extract, extracted_items[0])
            else:
                src_dir = tmp_extract

            if on_progress:
                on_progress(85, "Installing…")

            for item in os.listdir(src_dir):
                if item in PRESERVE:
                    log.info(f"[Updater] Preserving: {item}")
                    continue
                src = os.path.join(src_dir, item)
                dst = os.path.join(app_dir, item)
                try:
                    if os.path.isdir(src):
                        if os.path.exists(dst):
                            shutil.rmtree(dst)
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                except Exception as e:
                    log.warning(f"[Updater] Could not copy {item}: {e}")

            try:
                os.unlink(tmp_zip.name)
                shutil.rmtree(tmp_extract)
            except Exception:
                pass

            if on_progress:
                on_progress(100, "Update complete — restarting…")

            log.info("[Updater] Update installed — restarting app")

            if on_complete:
                on_complete()

            import time
            time.sleep(1.5)
            _restart()

        except Exception as e:
            log.error(f"[Updater] Update failed: {e}")
            if on_error:
                on_error(str(e))

    threading.Thread(target=_run, daemon=True).start()


def _restart():
    try:
        python = sys.executable
        args = sys.argv[:]
        log.info(f"[Updater] Restarting: {python} {args}")
        import subprocess
        subprocess.Popen([python] + args)
        os._exit(0)
    except Exception as e:
        log.error(f"[Updater] Restart failed: {e}")


def check_async(on_update_available=None):
    def _run():
        info = check_for_update()
        if info and on_update_available:
            on_update_available(info)

    threading.Thread(target=_run, daemon=True).start()

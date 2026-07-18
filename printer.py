"""
printer.py — ESC/POS receipt printing and image hot folder management
"""
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from typing import Optional

import requests as _requests

log = logging.getLogger("pdx.printer")

IS_WINDOWS = sys.platform == "win32"

LINE_WIDTH = 48  # TM-M30II 80mm, font A


def get_windows_printers() -> list:
    if not IS_WINDOWS:
        return []

    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Print\Printers"
        )
        printers = []
        i = 0
        while True:
            try:
                printers.append(winreg.EnumKey(key, i))
                i += 1
            except OSError:
                break
        winreg.CloseKey(key)
        if printers:
            return printers
    except Exception as e:
        log.warning(f"[Printer] winreg failed: {e}")

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Printer | Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=5
        )
        names = [n.strip() for n in result.stdout.strip().splitlines() if n.strip()]
        if names:
            return names
    except Exception as e:
        log.warning(f"[Printer] PowerShell failed: {e}")

    return []


def _fmt_dt(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        dt = dt.astimezone()
        h = dt.strftime("%I").lstrip("0") or "12"
        return dt.strftime(f"%b %d  {h}:%M %p")
    except Exception:
        return iso_str[:16]


def print_receipt(order: dict, printer_name: str, studio_name: str = "", logo_path: str = "") -> tuple[bool, str]:
    """Print an ESC/POS receipt to a Windows thermal printer (Epson TM-M30II or compatible)."""
    if not IS_WINDOWS:
        return False, "Printing only supported on Windows"
    if not printer_name:
        return False, "No printer configured"

    order_num   = order.get("order_num", "")
    customer    = order.get("customer_name", "Unknown")
    gallery     = order.get("gallery", "")
    placed_at   = order.get("placed_at", "")
    received_at = order.get("received_at", datetime.now().isoformat())

    try:
        items = json.loads(order.get("items_json", "[]"))
    except Exception:
        items = []

    images_by_idx: dict[int, list] = {}
    images_by_sku: dict[str, list] = {}
    try:
        images = json.loads(order.get("images_json", "[]"))
        for img in images:
            fname = img.get("filename", "")
            sku   = img.get("item_sku", "")
            images_by_sku.setdefault(sku, []).append(fname)
            idx = img.get("item_idx")
            if idx is not None:
                images_by_idx.setdefault(idx, []).append(fname)
    except Exception:
        pass

    SEP = "-" * LINE_WIDTH

    try:
        from escpos.printer import Win32Raw
        p = Win32Raw(printer_name)
        p.open()

        if studio_name:
            p.text(studio_name.upper() + "\n")
        p.text("PICKUP RECEIPT\n")
        p.text(SEP + "\n")
        p.text(customer + "\n")
        p.text(order_num + "\n")
        p.text(SEP + "\n")

        if items:
            p.text("ITEMS\n")
            shown_files = set()
            for item_pos, it in enumerate(items):
                qty  = it.get("qty", 1)
                desc = (it.get("desc") or it.get("sku") or "")[:LINE_WIDTH - 6]
                p.text(f"{qty}x  {desc}\n")
                fnames = images_by_idx.get(item_pos) or images_by_sku.get(it.get("sku", ""), [])
                for fname in fnames:
                    if fname not in shown_files:
                        shown_files.add(fname)
                        p.text(f"   -> {fname[:LINE_WIDTH - 7]}\n")
            p.text(SEP + "\n")

        if placed_at:
            p.text(f"Placed: {_fmt_dt(placed_at)}\n")

        p.text("\n")
        p.text(f"ORDER: {order_num}\n")
        p.text("Scan to confirm pickup\n")

        try:
            p.qr(order_num, native=True, size=5)
            p.text("\n")
        except Exception as qr_err:
            log.warning(f"[Printer] QR code failed: {qr_err}")

        p.text(SEP + "\n")
        p.text("\n\n\n")
        p.cut()
        p.close()

        log.info(f"[Printer] Receipt printed: {order_num}")
        return True, ""

    except Exception as e:
        log.error(f"[Printer] ESC/POS error for {order_num}: {e}")
        return False, str(e)


# ── Hot folder management ─────────────────────────────────────────────────────

def get_archive_folder(output_folder: str) -> str:
    return os.path.join(output_folder, "archive")


def get_order_archive_path(output_folder: str, order_num: str) -> str:
    return os.path.join(get_archive_folder(output_folder), order_num)


def fulfill_to_hot_folder(images: list, output_folder: str, order_num: str = "") -> tuple[bool, str]:
    """Move images from hot folder root to archive/ORDER_NUM/ subfolder."""
    if not output_folder:
        return False, "No image output folder configured"
    if not images:
        return False, "No images found for this order"

    archive = get_order_archive_path(output_folder, order_num) if order_num else get_archive_folder(output_folder)
    try:
        os.makedirs(archive, exist_ok=True)
    except Exception as e:
        return False, f"Cannot create archive folder: {e}"

    errors = []
    for img in images:
        filename = img.get("filename", "")
        item_qty = img.get("item_qty", 1)
        if not filename:
            continue

        # Build list: original + any qty copies
        filenames_to_move = [filename]
        if item_qty > 1:
            base, ext = os.path.splitext(filename)
            for copy_num in range(2, item_qty + 1):
                filenames_to_move.append(f"{base}_q{copy_num}{ext}")

        for fname in filenames_to_move:
            src = os.path.join(output_folder, fname)
            dst = os.path.join(archive, fname)
            if os.path.exists(src):
                try:
                    shutil.move(src, dst)
                    log.info(f"[HotFolder] Archived: {fname}")
                except Exception as e:
                    errors.append(f"{fname}: {e}")
            elif fname == filename:
                log.warning(f"[HotFolder] File not found to archive: {src}")

    if errors:
        return False, "; ".join(errors)
    return True, ""


def reprint_images_to_hot_folder(images: list, output_folder: str, order_num: str = "") -> tuple[bool, str]:
    """Copy from archive/ORDER_NUM/ back to hot folder root for reprinting."""
    if not output_folder:
        return False, "No image output folder configured"
    if not images:
        return False, "No images found for this order"

    archive_order = get_order_archive_path(output_folder, order_num) if order_num else None
    archive_flat  = get_archive_folder(output_folder)
    errors = []

    for img in images:
        filename = img.get("filename", "")
        item_qty = img.get("item_qty", 1)
        if not filename:
            continue
        dst = os.path.join(output_folder, filename)
        src = None
        if archive_order:
            candidate = os.path.join(archive_order, filename)
            if os.path.exists(candidate):
                src = candidate
        if not src:
            candidate = os.path.join(archive_flat, filename)
            if os.path.exists(candidate):
                src = candidate

        if src:
            try:
                shutil.copy2(src, dst)
                log.info(f"[HotFolder] Reprint: {filename}")
                # Recreate qty copies
                if item_qty > 1:
                    base, ext = os.path.splitext(filename)
                    for copy_num in range(2, item_qty + 1):
                        copy_name = f"{base}_q{copy_num}{ext}"
                        copy_dst = os.path.join(output_folder, copy_name)
                        if not os.path.exists(copy_dst):
                            try:
                                shutil.copy2(src, copy_dst)
                                log.info(f"[HotFolder] Reprint copy {copy_num}: {copy_name}")
                            except Exception as ce:
                                errors.append(f"{copy_name}: {ce}")
            except Exception as e:
                errors.append(f"{filename}: {e}")
        elif os.path.exists(dst):
            log.info(f"[HotFolder] Already in hot folder: {filename}")
        else:
            errors.append(f"{filename}: not found in archive")

    if errors:
        return False, "; ".join(errors)
    return True, ""


def download_images(images: list, output_folder: str, order_num: str = "", api_key: str = "") -> tuple[bool, str]:
    """Download images flat into hot folder root so DNP picks them up immediately."""
    if not output_folder:
        return False, "No image output folder configured"
    try:
        os.makedirs(output_folder, exist_ok=True)
    except Exception as e:
        return False, f"Cannot create output folder: {e}"
    if not images:
        return False, "No images found in order"

    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    errors = []
    for img in images:
        filename  = img.get("filename", "")
        asset_url = img.get("assetUrl", "")
        if not filename or not asset_url:
            errors.append("Missing filename or assetUrl")
            continue

        dest_path = os.path.join(output_folder, filename)

        archive_order_path = os.path.join(get_order_archive_path(output_folder, order_num), filename) if order_num else None
        archive_flat_path  = os.path.join(get_archive_folder(output_folder), filename)

        already_exists = (
            (os.path.exists(dest_path) and os.path.getsize(dest_path) > 0) or
            (archive_order_path and os.path.exists(archive_order_path) and os.path.getsize(archive_order_path) > 0) or
            (os.path.exists(archive_flat_path) and os.path.getsize(archive_flat_path) > 0)
        )
        if already_exists:
            log.info(f"[Download] Already exists, skipping: {filename}")
            continue

        try:
            resp = _requests.get(asset_url, headers=headers, timeout=30, stream=True)
            if not resp.ok:
                errors.append(f"{filename}: HTTP {resp.status_code}")
                continue
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    f.write(chunk)
            log.info(f"[Download] Saved: {filename} ({os.path.getsize(dest_path):,} bytes)")

            # Create additional copies for qty > 1 so DNP prints the correct number
            item_qty = img.get("item_qty", 1)
            if item_qty > 1:
                base, ext = os.path.splitext(filename)
                for copy_num in range(2, item_qty + 1):
                    copy_name = f"{base}_q{copy_num}{ext}"
                    copy_path = os.path.join(output_folder, copy_name)
                    if not os.path.exists(copy_path):
                        try:
                            shutil.copy2(dest_path, copy_path)
                            log.info(f"[Download] Qty copy {copy_num}: {copy_name}")
                        except Exception as ce:
                            errors.append(f"{copy_name}: {ce}")

        except _requests.exceptions.Timeout:
            errors.append(f"{filename}: timed out")
        except Exception as e:
            errors.append(f"{filename}: {e}")

    if errors:
        return False, "; ".join(errors)
    return True, ""


def get_image_path(filename: str, output_folder: str, order_num: str = "") -> Optional[str]:
    """Locate an image — hot folder, order archive, or flat archive."""
    if not output_folder or not filename:
        return None
    paths = [os.path.join(output_folder, filename)]
    if order_num:
        paths.append(os.path.join(get_order_archive_path(output_folder, order_num), filename))
    paths.append(os.path.join(get_archive_folder(output_folder), filename))
    for p in paths:
        if os.path.exists(p):
            return p
    return None

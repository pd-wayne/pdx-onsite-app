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


def _parse_order_data(order: dict):
    """Extract and parse all fields needed for receipt printing."""
    order_num = order.get("order_num", "")
    customer  = order.get("customer_name", "Unknown")
    placed_at = order.get("placed_at", "")
    try:
        items = json.loads(order.get("items_json", "[]"))
    except Exception:
        items = []
    images_by_idx: dict[int, list] = {}
    images_by_sku: dict[str, list] = {}
    try:
        for img in json.loads(order.get("images_json", "[]")):
            fname = img.get("filename", "")
            sku   = img.get("item_sku", "")
            images_by_sku.setdefault(sku, []).append(fname)
            idx = img.get("item_idx")
            if idx is not None:
                images_by_idx.setdefault(idx, []).append(fname)
    except Exception:
        pass
    return order_num, customer, placed_at, items, images_by_idx, images_by_sku


def _print_receipt_gdi(order_num, customer, placed_at, items, images_by_idx,
                       images_by_sku, printer_name, studio_name) -> tuple[bool, str]:
    """Print receipt using Windows GDI — renders text + QR as raster, works with any driver."""
    try:
        import win32ui
        import qrcode
        from PIL import Image, ImageWin

        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)

        dpi_x = hdc.GetDeviceCaps(88)   # LOGPIXELSX
        dpi_y = hdc.GetDeviceCaps(90)   # LOGPIXELSY
        pw    = hdc.GetDeviceCaps(8)    # HORZRES (printable width in pixels)

        font_h = -int(dpi_y * 9 / 72)  # 9pt
        font = win32ui.CreateFont({"name": "Courier New", "height": font_h, "weight": 400})

        hdc.StartDoc("PDX Onsite Receipt")
        hdc.StartPage()
        hdc.SelectObject(font)

        tm     = hdc.GetTextMetrics()
        line_h = tm["Height"] + tm["ExternalLeading"]
        char_w, _ = hdc.GetTextExtent("M")
        line_chars = max(20, pw // char_w)
        SEP = "-" * line_chars

        y = 0

        def out(text):
            nonlocal y
            hdc.TextOut(0, y, str(text))
            y += line_h

        if studio_name:
            out(studio_name.upper())
        out("PICKUP RECEIPT")
        out(SEP)
        out(customer)
        out(order_num)
        out(SEP)

        if items:
            out("ITEMS")
            shown = set()
            for i, it in enumerate(items):
                qty  = it.get("qty", 1)
                desc = (it.get("desc") or it.get("sku") or "")[:line_chars - 6]
                out(f"{qty}x  {desc}")
                fnames = images_by_idx.get(i) or images_by_sku.get(it.get("sku", ""), [])
                for fname in fnames:
                    if fname not in shown:
                        shown.add(fname)
                        out(f"   -> {fname[:line_chars - 7]}")
            out(SEP)

        if placed_at:
            out(f"Placed: {_fmt_dt(placed_at)}")

        out("")
        out(f"ORDER: {order_num}")
        out("Scan to confirm pickup")
        out("")

        # QR code rendered as bitmap — bypasses ESC/POS command issues entirely
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=6, border=2,
        )
        qr.add_data(order_num)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_size = int(dpi_x * 1.5)  # 1.5 inches
        qr_img  = qr_img.resize((qr_size, qr_size), Image.NEAREST)
        x_qr    = max(0, (pw - qr_size) // 2)

        dib = ImageWin.Dib(qr_img)
        dib.draw(hdc.GetSafeHdc(), (x_qr, y, x_qr + qr_size, y + qr_size))
        y += qr_size + line_h

        out(SEP)

        hdc.EndPage()
        hdc.EndDoc()
        hdc.DeleteDC()

        return True, ""

    except Exception as e:
        log.warning(f"[Printer] GDI print failed: {e}")
        return False, str(e)


def _print_receipt_escpos(order_num, customer, placed_at, items, images_by_idx,
                          images_by_sku, printer_name, studio_name) -> tuple[bool, str]:
    """Plain ASCII ESC/POS fallback — no QR code, works even if GDI is unavailable."""
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
            shown = set()
            for item_pos, it in enumerate(items):
                qty  = it.get("qty", 1)
                desc = (it.get("desc") or it.get("sku") or "")[:LINE_WIDTH - 6]
                p.text(f"{qty}x  {desc}\n")
                fnames = images_by_idx.get(item_pos) or images_by_sku.get(it.get("sku", ""), [])
                for fname in fnames:
                    if fname not in shown:
                        shown.add(fname)
                        p.text(f"   -> {fname[:LINE_WIDTH - 7]}\n")
            p.text(SEP + "\n")

        if placed_at:
            p.text(f"Placed: {_fmt_dt(placed_at)}\n")

        p.text("\n")
        p.text(f"ORDER: {order_num}\n")
        p.text("Scan to confirm pickup\n")
        p.text(SEP + "\n")
        p.text("\n\n\n")
        p.cut()
        p.close()

        return True, ""

    except Exception as e:
        log.error(f"[Printer] ESC/POS error for {order_num}: {e}")
        return False, str(e)


def print_receipt(order: dict, printer_name: str, studio_name: str = "", logo_path: str = "") -> tuple[bool, str]:
    """Print receipt to a Windows thermal printer. Tries GDI (with QR) first, falls back to ESC/POS text."""
    if not IS_WINDOWS:
        return False, "Printing only supported on Windows"
    if not printer_name:
        return False, "No printer configured"

    order_num, customer, placed_at, items, images_by_idx, images_by_sku = _parse_order_data(order)

    ok, err = _print_receipt_gdi(order_num, customer, placed_at, items,
                                  images_by_idx, images_by_sku, printer_name, studio_name)
    if ok:
        log.info(f"[Printer] Receipt printed (GDI+QR): {order_num}")
        return True, ""

    log.warning(f"[Printer] GDI failed ({err}), falling back to ESC/POS text")
    ok, err = _print_receipt_escpos(order_num, customer, placed_at, items,
                                     images_by_idx, images_by_sku, printer_name, studio_name)
    if ok:
        log.info(f"[Printer] Receipt printed (ESC/POS text): {order_num}")
    return ok, err


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

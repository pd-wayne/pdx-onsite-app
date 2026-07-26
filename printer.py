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
    order_num      = order.get("order_num", "")
    customer       = order.get("customer_name", "Unknown")
    customer_phone = order.get("customer_phone", "")
    placed_at      = order.get("placed_at", "")
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
    return order_num, customer, customer_phone, placed_at, items, images_by_idx, images_by_sku


def _print_receipt_gdi_v1(order_num, customer, placed_at, items, images_by_idx,
                          images_by_sku, printer_name, studio_name) -> tuple[bool, str]:
    """Legacy GDI receipt layout — kept as reversion fallback. Do not delete."""
    try:
        import win32ui
        import qrcode
        from PIL import Image, ImageDraw, ImageFont, ImageWin

        # ── Probe printer DPI and printable width ────────────────────────────
        hdc_probe = win32ui.CreateDC()
        hdc_probe.CreatePrinterDC(printer_name)
        dpi_x = hdc_probe.GetDeviceCaps(88)   # LOGPIXELSX
        dpi_y = hdc_probe.GetDeviceCaps(90)   # LOGPIXELSY
        pw    = hdc_probe.GetDeviceCaps(8)    # HORZRES (printable width)
        hdc_probe.DeleteDC()

        # ── Font (Windows system Courier New — always present) ────────────────
        pt = 9
        font_px = max(12, int(dpi_y * pt / 72))
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/cour.ttf", font_px)
        except Exception:
            font = ImageFont.load_default()

        line_h = int(font_px * 1.35)
        # Estimate chars per line from average char width (~0.6× height for Courier)
        char_w = max(1, int(font_px * 0.6))
        line_chars = max(20, pw // char_w)
        SEP = "-" * line_chars

        # ── Build receipt lines ───────────────────────────────────────────────
        lines = []
        if studio_name:
            lines.append(studio_name.upper())
        lines.append("PICKUP RECEIPT")
        lines.append(SEP)
        lines.append(customer)
        lines.append(order_num)
        lines.append(SEP)

        if items:
            lines.append("ITEMS")
            shown = set()
            for i, it in enumerate(items):
                qty  = it.get("qty", 1)
                desc = (it.get("desc") or it.get("sku") or "")[:line_chars - 6]
                lines.append(f"{qty}x  {desc}")
                fnames = images_by_idx.get(i) or images_by_sku.get(it.get("sku", ""), [])
                for fname in fnames:
                    if fname not in shown:
                        shown.add(fname)
                        lines.append(f"   -> {fname[:line_chars - 7]}")
            lines.append(SEP)

        if placed_at:
            lines.append(f"Placed: {_fmt_dt(placed_at)}")
        lines.append("")
        lines.append(f"ORDER: {order_num}")
        lines.append("Scan to confirm pickup")
        lines.append("")

        # ── Build QR image ────────────────────────────────────────────────────
        # Compute box_size so the QR prints at ~2 inches without any resize step.
        # Resizing blurs module edges; generating at target size keeps them sharp.
        target_px = int(dpi_x * 2.0)   # 2 inches
        # Version 2 QR (worst case for short order#): 25 modules + 2*4 quiet zone = 33
        box_size  = max(6, target_px // 33)
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=box_size, border=4,
        )
        qr.add_data(order_num)
        qr.make(fit=True)
        qr_img  = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_size = qr_img.size[0]   # use actual generated size, no resize

        # ── Compose full receipt as PIL image ─────────────────────────────────
        total_h = len(lines) * line_h + qr_size + line_h * 2
        img  = Image.new("RGB", (pw, total_h), "white")
        draw = ImageDraw.Draw(img)

        y = 0
        for line in lines:
            draw.text((0, y), line, fill="black", font=font)
            y += line_h

        x_qr = max(0, (pw - qr_size) // 2)
        img.paste(qr_img, (x_qr, y))
        y += qr_size + 4

        draw.text((0, y), SEP, fill="black", font=font)

        # ── Send to printer via GDI ───────────────────────────────────────────
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)
        hdc.StartDoc("PDX Onsite Receipt")
        hdc.StartPage()

        dib = ImageWin.Dib(img)
        dib.draw(hdc.GetSafeHdc(), (0, 0, pw, total_h))

        hdc.EndPage()
        hdc.EndDoc()
        hdc.DeleteDC()

        return True, ""

    except Exception as e:
        log.warning(f"[Printer] GDI print failed: {e}")
        return False, str(e)


def _print_receipt_gdi(order_num, customer, customer_phone, placed_at, items,
                       images_by_idx, images_by_sku, printer_name, studio_name) -> tuple[bool, str]:
    """
    Print receipt using Windows GDI + PIL.
    Two-column label/value layout, dashed order-number box, Arial Bold fonts.
    Renders the full receipt as a raster image and sends via ImageWin.Dib —
    bypasses ESC/POS entirely so it works with any Windows printer driver.
    """
    try:
        import win32ui
        import qrcode
        from PIL import Image, ImageDraw, ImageFont, ImageWin

        # ── Probe printer DPI and printable width ─────────────────────────────
        hdc_probe = win32ui.CreateDC()
        hdc_probe.CreatePrinterDC(printer_name)
        dpi_x = hdc_probe.GetDeviceCaps(88)
        dpi_y = hdc_probe.GetDeviceCaps(90)
        pw    = hdc_probe.GetDeviceCaps(8)
        hdc_probe.DeleteDC()

        pad = max(10, int(pw * 0.05))

        def _pt(points):
            return max(10, int(dpi_y * points / 72))

        try:
            f_large = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", _pt(12))
            f_body  = ImageFont.truetype("C:/Windows/Fonts/arial.ttf",   _pt(8))
            f_bold  = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", _pt(8))
            f_small = ImageFont.truetype("C:/Windows/Fonts/arial.ttf",   _pt(7))
        except Exception:
            f_large = f_body = f_bold = f_small = ImageFont.load_default()

        lh_large = int(_pt(12) * 1.35)
        lh_body  = int(_pt(8)  * 1.35)
        lh_small = int(_pt(7)  * 1.30)
        gap      = max(4, lh_body // 3)

        # ── Build QR image ────────────────────────────────────────────────────
        target_px = int(dpi_x * 2.0)
        box_size  = max(6, target_px // 33)
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=box_size, border=4,
        )
        qr.add_data(order_num)
        qr.make(fit=True)
        qr_img  = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        qr_size = qr_img.size[0]

        # ── Oversized canvas — will crop to content after drawing ─────────────
        items_count = len(items) if items else 0
        max_h = max(4000, qr_size + lh_body * (items_count * 6 + 40) + 800)
        img  = Image.new("RGB", (pw, max_h), "white")
        draw = ImageDraw.Draw(img)

        # ── Drawing helpers ───────────────────────────────────────────────────
        def tw(text, font):
            return int(font.getlength(text))

        def centered(text, y, font, fill="black"):
            draw.text(((pw - tw(text, font)) // 2, y), text, fill=fill, font=font)

        def two_col(label, value, y, val_font=None):
            vf = val_font or f_bold
            draw.text((pad, y), label, fill="#888888", font=f_body)
            draw.text((pw - pad - tw(value, vf), y), value, fill="black", font=vf)

        def dashed_sep(y):
            dash, sp = 5, 4
            x = pad
            while x < pw - pad:
                draw.line([(x, y), (min(x + dash, pw - pad), y)], fill="#aaaaaa", width=1)
                x += dash + sp

        def dashed_rect(x0, y0, x1, y1):
            dash, sp = 7, 5
            for seg in [(x0, y0, x1, y0), (x0, y1, x1, y1)]:  # top, bottom
                x = seg[0]
                while x < seg[2]:
                    draw.line([(x, seg[1]), (min(x + dash, seg[2]), seg[1])],
                              fill="black", width=2)
                    x += dash + sp
            for seg in [(x0, y0, x0, y1), (x1, y0, x1, y1)]:  # left, right
                yc = seg[1]
                while yc < seg[3]:
                    draw.line([(seg[0], yc), (seg[0], min(yc + dash, seg[3]))],
                              fill="black", width=2)
                    yc += dash + sp

        def fit_text(text, font, max_w):
            """Truncate text to fit within max_w pixels."""
            while text and tw(text, font) > max_w:
                text = text[:-1]
            return text

        y = gap * 2

        # ── Studio name header ────────────────────────────────────────────────
        if studio_name:
            centered(studio_name.upper(), y, f_large)
            y += lh_large
        centered("PICKUP RECEIPT", y, f_small, fill="#888888")
        y += lh_small
        if placed_at:
            centered(_fmt_dt(placed_at), y, f_small, fill="#aaaaaa")
            y += lh_small
        y += gap
        dashed_sep(y)
        y += lh_small + gap

        # ── Order number box ──────────────────────────────────────────────────
        inner_pad  = max(6, lh_body // 2)
        label_text = "ORDER NUMBER"
        box_h = inner_pad + lh_small + gap // 2 + lh_large + inner_pad
        dashed_rect(pad, y, pw - pad, y + box_h)
        centered(label_text, y + inner_pad, f_small, fill="#888888")
        centered(order_num, y + inner_pad + lh_small + gap // 2, f_large)
        y += box_h
        y += gap
        dashed_sep(y)
        y += lh_small + gap

        # ── Customer info ─────────────────────────────────────────────────────
        two_col("Customer", customer, y)
        y += lh_body
        if customer_phone:
            two_col("Phone", customer_phone, y)
            y += lh_body
        y += gap
        dashed_sep(y)
        y += lh_small + gap

        # ── Items ─────────────────────────────────────────────────────────────
        if items:
            draw.text((pad, y), "ITEMS", fill="#888888", font=f_small)
            y += lh_small + gap // 2
            shown = set()
            for i, it in enumerate(items):
                qty  = it.get("qty", 1)
                qty_str = f"x{qty}"
                desc = it.get("desc") or it.get("sku") or ""
                desc = fit_text(desc, f_bold, pw - pad * 2 - tw(qty_str, f_bold) - 20)
                two_col(desc, qty_str, y, val_font=f_bold)
                y += lh_body
                fnames = images_by_idx.get(i) or images_by_sku.get(it.get("sku", ""), [])
                for fname in fnames:
                    if fname not in shown:
                        shown.add(fname)
                        fn = fit_text(fname, f_small, pw - pad * 2 - 16)
                        draw.text((pad + 16, y), fn, fill="#aaaaaa", font=f_small)
                        y += lh_small
            y += gap
            dashed_sep(y)
            y += lh_small + gap

        # ── QR code ───────────────────────────────────────────────────────────
        centered("Scan to confirm pickup", y, f_small, fill="#888888")
        y += lh_small + gap
        img.paste(qr_img, (max(0, (pw - qr_size) // 2), y))
        y += qr_size + gap
        dashed_sep(y)
        y += lh_small + gap

        # ── Footer studio name ────────────────────────────────────────────────
        if studio_name:
            centered(studio_name.upper(), y, f_large)
            y += lh_large
        y += gap * 2

        # ── Crop and print ────────────────────────────────────────────────────
        final_h = y
        img = img.crop((0, 0, pw, final_h))

        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)
        hdc.StartDoc("PDX Onsite Receipt")
        hdc.StartPage()
        dib = ImageWin.Dib(img)
        dib.draw(hdc.GetSafeHdc(), (0, 0, pw, final_h))
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

    order_num, customer, customer_phone, placed_at, items, images_by_idx, images_by_sku = _parse_order_data(order)

    ok, err = _print_receipt_gdi(order_num, customer, customer_phone, placed_at, items,
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

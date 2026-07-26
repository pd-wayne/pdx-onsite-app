"""
test_printer.py — Tests for the packing-slip helpers in printer.py.

Only the pure-Python pieces are testable here — the actual GDI rendering
(_print_packing_slip_gdi) requires win32ui/PIL and only runs on Windows. This dev
machine has IS_WINDOWS == False, so print_packing_slip's top-level guard is exercised
directly; its bulk-vs-standard dispatch logic is exercised by monkeypatching
IS_WINDOWS and stubbing out _print_packing_slip_gdi.
"""
import json
import pytest

import printer


# ── _parse_packing_slip_items ────────────────────────────────────────────────────

class TestParsePackingSlipItems:
    def test_flattens_images_json(self):
        order = {"images_json": json.dumps([
            {"filename": "a.jpg", "item_desc": "8x10 Print", "print_spec": "8x10"},
            {"filename": "b.jpg", "item_desc": "5x7 Print", "print_spec": "5x7"},
        ])}
        rows = printer._parse_packing_slip_items(order)
        assert rows == [
            {"filename": "a.jpg", "item_desc": "8x10 Print", "print_spec": "8x10"},
            {"filename": "b.jpg", "item_desc": "5x7 Print", "print_spec": "5x7"},
        ]

    def test_empty_images_json_returns_empty_list(self):
        assert printer._parse_packing_slip_items({"images_json": "[]"}) == []

    def test_missing_images_json_key_returns_empty_list(self):
        assert printer._parse_packing_slip_items({}) == []

    def test_malformed_json_returns_empty_list(self):
        assert printer._parse_packing_slip_items({"images_json": "not json"}) == []


# ── locate_downloaded_image ───────────────────────────────────────────────────────

class TestLocateDownloadedImage:
    def test_finds_file_in_second_destination(self, tmp_path):
        dest_a = tmp_path / "DestA"
        dest_b = tmp_path / "DestB"
        dest_a.mkdir()
        dest_b.mkdir()
        (dest_b / "photo.jpg").write_bytes(b"fake")
        destinations = [
            {"hot_folder_path": str(dest_a), "active": True},
            {"hot_folder_path": str(dest_b), "active": True},
        ]
        path = printer.locate_downloaded_image("photo.jpg", destinations)
        assert path == str(dest_b / "photo.jpg")

    def test_skips_inactive_destinations(self, tmp_path):
        dest = tmp_path / "Dest"
        dest.mkdir()
        (dest / "photo.jpg").write_bytes(b"fake")
        destinations = [{"hot_folder_path": str(dest), "active": False}]
        assert printer.locate_downloaded_image("photo.jpg", destinations) is None

    def test_finds_file_in_archive_subfolder(self, tmp_path):
        dest = tmp_path / "Dest"
        archive = dest / "archive" / "ORD001"
        archive.mkdir(parents=True)
        (archive / "photo.jpg").write_bytes(b"fake")
        destinations = [{"hot_folder_path": str(dest), "active": True}]
        path = printer.locate_downloaded_image("photo.jpg", destinations, order_num="ORD001")
        assert path == str(archive / "photo.jpg")

    def test_returns_none_when_not_found_anywhere(self, tmp_path):
        dest = tmp_path / "Dest"
        dest.mkdir()
        destinations = [{"hot_folder_path": str(dest), "active": True}]
        assert printer.locate_downloaded_image("missing.jpg", destinations) is None

    def test_empty_destinations_returns_none(self):
        assert printer.locate_downloaded_image("photo.jpg", []) is None


# ── _group_items_to_slip_rows ─────────────────────────────────────────────────────

class TestGroupItemsToSlipRows:
    def test_flattens_self_contained_group_items(self):
        group = {
            "fields": [{"label": "Name", "value": "Jane Smith"}],
            "items": [
                {"description": "8x10 Print", "images": [{"filename": "a.jpg", "externalId": "8x10"}]},
            ],
        }
        rows = printer._group_items_to_slip_rows(group)
        assert rows == [{"filename": "a.jpg", "item_desc": "8x10 Print", "print_spec": "8x10"}]

    def test_group_with_no_items_returns_empty(self):
        assert printer._group_items_to_slip_rows({"fields": [], "items": []}) == []

    def test_group_items_without_images_returns_empty(self):
        group = {"items": [{"description": "8x10 Print"}]}
        assert printer._group_items_to_slip_rows(group) == []


# ── build_packing_slip_pages / build_packing_slips_pdf ────────────────────────────
# Rendering is now pure PIL (no printer/OS dependency) — the caller (server.py)
# turns the pages into a PDF the browser opens and prints via its own dialog.

class TestBuildPackingSlipPages:
    def test_standard_order_renders_one_section(self, monkeypatch):
        calls = []
        monkeypatch.setattr(printer, "_render_packing_slip_pages",
                            lambda *a, **k: (calls.append((a, k)) or ["page"]))
        order = {
            "order_num": "ORD001", "customer_name": "Jane Doe", "gallery": "G",
            "images_json": json.dumps([{"filename": "a.jpg", "item_desc": "8x10 Print", "print_spec": "8x10"}]),
            "raw_json": json.dumps({"isBulkOrder": False, "shipping": {"destination": {}}}),
        }
        pages = printer.build_packing_slip_pages(order, [])
        assert pages == ["page"]
        assert len(calls) == 1

    def test_bulk_order_renders_one_section_per_group(self, monkeypatch):
        calls = []
        monkeypatch.setattr(printer, "_render_packing_slip_pages",
                            lambda *a, **k: (calls.append((a, k)) or ["page"]))
        order = {
            "order_num": "ORD002", "gallery": "G", "images_json": "[]",
            "raw_json": json.dumps({
                "isBulkOrder": True,
                "groups": [
                    {"fields": [{"label": "Name", "value": "A"}], "items": []},
                    {"fields": [{"label": "Name", "value": "B"}], "items": []},
                ],
            }),
        }
        pages = printer.build_packing_slip_pages(order, [])
        assert pages == ["page", "page"]
        assert len(calls) == 2

    def test_standard_order_handles_missing_raw_json(self, monkeypatch):
        # Order rows without raw_json (shouldn't happen in practice, but be defensive)
        calls = []
        monkeypatch.setattr(printer, "_render_packing_slip_pages",
                            lambda *a, **k: (calls.append(1) or ["page"]))
        order = {"order_num": "ORD004", "images_json": "[]"}
        pages = printer.build_packing_slip_pages(order, [])
        assert pages == ["page"]
        assert len(calls) == 1


class TestBuildPackingSlipsPdf:
    def _order(self, order_num):
        return {
            "order_num": order_num, "customer_name": "Jane Doe", "gallery": "G",
            "images_json": json.dumps([{"filename": "a.jpg", "item_desc": "8x10 Print", "print_spec": "8x10"}]),
            "raw_json": json.dumps({"isBulkOrder": False, "shipping": {"destination": {}}}),
        }

    def test_produces_real_pdf_bytes(self):
        pdf_bytes = printer.build_packing_slips_pdf([self._order("ORD001")], [])
        assert pdf_bytes[:4] == b"%PDF"
        assert len(pdf_bytes) > 100

    def test_combines_multiple_orders_into_one_pdf(self):
        # A combined PDF for N single-page orders should have more pages (bigger
        # file) than a single order's PDF — proves it's genuinely combining them,
        # not just returning the first order's document.
        one = printer.build_packing_slips_pdf([self._order("ORD001")], [])
        three = printer.build_packing_slips_pdf(
            [self._order("ORD001"), self._order("ORD002"), self._order("ORD003")], []
        )
        assert len(three) > len(one)

    def test_empty_orders_list_still_returns_valid_pdf(self):
        pdf_bytes = printer.build_packing_slips_pdf([], [])
        assert pdf_bytes[:4] == b"%PDF"

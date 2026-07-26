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


# ── _raw_items_to_slip_rows / _group_label_and_display_fields ─────────────────────
# Confirmed against a live PDX bulk order sample: groups[] entries carry only
# {id, fields} — no items of their own. The real linkage is item.groupId ==
# group.id on the order's top-level items[], and fields use "key" (snake_case),
# not "label".

class TestRawItemsToSlipRows:
    def test_flattens_raw_items_with_images(self):
        items = [{"description": "8x10 Print", "images": [{"filename": "a.jpg", "externalId": "8x10"}]}]
        rows = printer._raw_items_to_slip_rows(items)
        assert rows == [{"filename": "a.jpg", "item_desc": "8x10 Print", "print_spec": "8x10"}]

    def test_empty_items_returns_empty(self):
        assert printer._raw_items_to_slip_rows([]) == []

    def test_items_without_images_returns_empty(self):
        assert printer._raw_items_to_slip_rows([{"description": "8x10 Print"}]) == []


class TestGroupLabelAndDisplayFields:
    def test_first_last_name_becomes_label_and_is_excluded_from_display(self):
        fields = [
            {"key": "last_name", "value": "Testerson"},
            {"key": "first_name", "value": "Alex"},
            {"key": "num", "value": "HE1752168291"},
        ]
        label, display = printer._group_label_and_display_fields(fields)
        assert label == "Alex Testerson"
        assert display == [{"key": "num", "value": "HE1752168291"}]

    def test_no_name_fields_joins_all_values_and_keeps_them_for_display(self):
        fields = [{"key": "grade", "value": "3"}, {"key": "teacher", "value": "Ms. Lee"}]
        label, display = printer._group_label_and_display_fields(fields)
        assert label == "3 / Ms. Lee"
        assert display == fields

    def test_empty_fields_returns_group_fallback_label(self):
        label, display = printer._group_label_and_display_fields([])
        assert label == "Group"
        assert display == []


class TestHumanizeFieldKey:
    def test_snake_case_key_becomes_title_case(self):
        assert printer._humanize_field_key("last_name") == "Last Name"
        assert printer._humanize_field_key("num") == "Num"


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
        # Real shape (confirmed against a live PDX bulk sample): groups[] carry only
        # {id, fields} — items are matched via item.groupId == group.id, not nested
        # inside the group. No isBulkOrder flag needed/present — non-empty groups
        # alone signals a bulk/roster order.
        calls = []
        monkeypatch.setattr(printer, "_render_packing_slip_pages",
                            lambda *a, **k: (calls.append((a, k)) or ["page"]))
        order = {
            "order_num": "ORD002", "gallery": "G", "images_json": "[]",
            "raw_json": json.dumps({
                "items": [
                    {"groupId": 1, "description": "Item A", "images": [{"filename": "a.jpg", "externalId": "8x10"}]},
                    {"groupId": 2, "description": "Item B", "images": [{"filename": "b.jpg", "externalId": "5x7"}]},
                ],
                "groups": [
                    {"id": 1, "fields": [{"key": "first_name", "value": "A"}]},
                    {"id": 2, "fields": [{"key": "first_name", "value": "B"}]},
                ],
            }),
        }
        pages = printer.build_packing_slip_pages(order, [])
        assert pages == ["page", "page"]
        assert len(calls) == 2

    def test_group_with_no_matching_items_falls_back_to_full_order(self, monkeypatch):
        calls = []
        monkeypatch.setattr(printer, "_render_packing_slip_pages",
                            lambda order_num, header, rows, *a, **k: (calls.append(rows) or ["page"]))
        order = {
            "order_num": "ORD003", "gallery": "G",
            "images_json": json.dumps([{"filename": "z.jpg", "item_desc": "Fallback Item", "print_spec": "5x7"}]),
            "raw_json": json.dumps({
                "items": [],  # no items carry a matching groupId
                "groups": [{"id": 99, "fields": [{"key": "first_name", "value": "Nobody"}]}],
            }),
        }
        printer.build_packing_slip_pages(order, [])
        assert calls == [[{"filename": "z.jpg", "item_desc": "Fallback Item", "print_spec": "5x7"}]]

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


# ── Real bulk order sample (structure only — names redacted) ──────────────────────
# Shape confirmed against a live PDX bulk/roster order export. Two groups, sharing
# one person across two roster entries (a common case: one student appears in two
# separate rosters/events within the same bulk shipment) — group 1 has two items
# (a plaque + a print), group 2 has one (just a plaque), split via item.groupId.

REAL_BULK_RAW_JSON = json.dumps({
    "id": "d7b3a595-0a4d-4cae-8a58-82e855e48ec1", "num": "BAP1752155240",
    "items": [
        {"id": "i1", "groupId": 1, "quantity": 1, "externalId": "mm912",
         "description": "Memory Mate Plaque 9x12",
         "images": [{"assetUrl": "https://x/a.jpg", "filename": "a.jpg", "externalId": "10000", "orientation": "vertical"}]},
        {"id": "i2", "groupId": 1, "quantity": 1, "externalId": "print1218",
         "description": "Print 12x18",
         "images": [{"assetUrl": "https://x/b.jpg", "filename": "b.jpg", "externalId": "10000", "orientation": "vertical"}]},
        {"id": "i3", "groupId": 2, "quantity": 1, "externalId": "mm912",
         "description": "Memory Mate Plaque 9x12",
         "images": [{"assetUrl": "https://x/c.jpg", "filename": "c.jpg", "externalId": "10000", "orientation": "vertical"}]},
    ],
    "groups": [
        {"id": 1, "fields": [{"key": "last_name", "value": "Doe"}, {"key": "first_name", "value": "Jamie"},
                              {"key": "num", "value": "HE1752168291"}]},
        {"id": 2, "fields": [{"key": "last_name", "value": "Doe"}, {"key": "first_name", "value": "Jamie"},
                              {"key": "num", "value": "TX1752173992"}]},
    ],
    "gallery": "PDX Bulk Drop", "placedAt": "2025-07-11T07:00:00.000Z",
    "shipping": {"destination": {"recipient": "Production Facility"}},
})


class TestRealBulkOrderSample:
    def _order(self):
        return {
            "order_num": "BAP1752155240", "gallery": "PDX Bulk Drop", "status": "received",
            "images_json": json.dumps([
                {"filename": "a.jpg", "item_desc": "Memory Mate Plaque 9x12", "print_spec": "10000"},
                {"filename": "b.jpg", "item_desc": "Print 12x18", "print_spec": "10000"},
                {"filename": "c.jpg", "item_desc": "Memory Mate Plaque 9x12", "print_spec": "10000"},
            ]),
            "raw_json": REAL_BULK_RAW_JSON,
        }

    def test_renders_one_page_per_roster_group(self):
        pages = printer.build_packing_slip_pages(self._order(), [])
        assert len(pages) == 2  # one per groups[] entry, not per item

    def test_produces_a_real_multi_page_pdf(self):
        pdf_bytes = printer.build_packing_slips_pdf([self._order()], [])
        assert pdf_bytes[:4] == b"%PDF"

    def test_group_matching_splits_items_correctly(self, monkeypatch):
        # group 1 (2 items via groupId) vs group 2 (1 item) — not all 3 on every slip
        captured_rows = []
        monkeypatch.setattr(printer, "_render_packing_slip_pages",
                            lambda order_num, header, rows, *a, **k: (captured_rows.append(rows) or ["page"]))
        printer.build_packing_slip_pages(self._order(), [])
        assert len(captured_rows[0]) == 2
        assert len(captured_rows[1]) == 1

    def test_group_label_uses_first_last_name_not_raw_field_dump(self, monkeypatch):
        captured_headers = []
        monkeypatch.setattr(printer, "_render_packing_slip_pages",
                            lambda order_num, header, rows, *a, **k: (captured_headers.append(header) or ["page"]))
        printer.build_packing_slip_pages(self._order(), [])
        assert captured_headers[0]["group_label"] == "Jamie Doe"
        # first/last name excluded from the field list below the label — only "num" remains
        assert captured_headers[0]["fields"] == [{"key": "num", "value": "HE1752168291"}]


class TestNonBulkOrderWithGroupIdButEmptyGroups:
    """A second real PDX sample (order ZI1784299034): every item carries a
    groupId even on a plain, non-grouped order — isBulkOrder is explicitly
    False AND groups is simply []. Confirms groupId presence alone must never
    trigger group-splitting; only a non-empty groups[] does."""

    def _order(self):
        raw = {
            "id": "cf5679e1", "num": "ZI1784299034",
            "items": [
                {"id": "ae76dc36", "groupId": 1, "quantity": 1, "externalId": "5555",
                 "description": "5x7 Print",
                 "images": [{"filename": "a.jpg", "externalId": "5X7", "orientation": "vertical"}]},
                {"id": "ae6f19f1", "groupId": 1, "quantity": 1, "externalId": "mem-mat-8x10",
                 "description": "Memory Mate Horizontal 10x8",
                 "images": [{"filename": "b.jpg", "externalId": "mmh-1008", "orientation": "horizontal"}]},
            ],
            "groups": [],
            "gallery": "PDX Bulk Test", "placedAt": "2026-07-17T14:39:39.375Z",
            "shipping": {"option": {"externalId": "pdx_ECON"},
                        "destination": {"recipient": "Martha R. Piovesan", "city": "Winter park",
                                        "state": "FL", "zipCode": "32789-2919"}},
            "isBulkOrder": False, "pricingGroup": None, "totalLabCostCents": 30,
        }
        return {
            "order_num": "ZI1784299034", "gallery": "PDX Bulk Test", "status": "received",
            "customer_name": "Martha R. Piovesan",
            "images_json": json.dumps([
                {"filename": "a.jpg", "item_desc": "5x7 Print", "print_spec": "5X7"},
                {"filename": "b.jpg", "item_desc": "Memory Mate Horizontal 10x8", "print_spec": "mmh-1008"},
            ]),
            "raw_json": json.dumps(raw),
        }

    def test_renders_as_a_single_standard_slip_not_split(self):
        pages = printer.build_packing_slip_pages(self._order(), [])
        assert len(pages) == 1

    def test_standard_header_used_not_group_label(self, monkeypatch):
        captured_headers = []
        monkeypatch.setattr(printer, "_render_packing_slip_pages",
                            lambda order_num, header, rows, *a, **k: (captured_headers.append(header) or ["page"]))
        printer.build_packing_slip_pages(self._order(), [])
        assert "group_label" not in captured_headers[0]
        assert captured_headers[0]["customer"] == "Martha R. Piovesan"

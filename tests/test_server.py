"""
test_server.py — Flask route integration tests.

Uses Flask's test client with an isolated DB and mock poller.
Every test gets a fresh DB via the `client` fixture in conftest.py.
"""
import io
import json
import threading
import pytest
import config
import db


# ── Frontend ──────────────────────────────────────────────────────────────────

class TestFrontend:
    def test_index_serves_html(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"PDX Onsite" in resp.data

    def test_static_css_served(self, client):
        resp = client.get("/static/styles.css")
        assert resp.status_code == 200
        assert b"--bg" in resp.data   # CSS variable present

    def test_static_js_served(self, client):
        resp = client.get("/static/app.js")
        assert resp.status_code == 200
        assert b"function" in resp.data

    def test_missing_static_file_returns_404(self, client):
        assert client.get("/static/doesnotexist.xyz").status_code == 404


# ── Settings ──────────────────────────────────────────────────────────────────

class TestSettings:
    def test_get_settings_returns_defaults(self, client):
        resp = client.get("/api/get_settings")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["lab_id"] == ""

    def test_get_settings_contains_all_expected_keys(self, client):
        data = client.get("/api/get_settings").get_json()
        for key in ("lab_id", "api_key", "studio_name", "poll_interval",
                    "printer_name", "image_output_folder"):
            assert key in data, f"Missing key: {key}"

    def test_save_settings_returns_ok(self, client):
        resp = client.post("/api/save_settings",
                           data=json.dumps({"lab_id": "x", "poll_interval": 60}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_partial_save_does_not_blank_live_poller_credentials(self, client):
        """Regression test: save_settings used to reconfigure the poller from
        the raw POSTED payload (data.get("lab_id", "")), not the merged saved
        config. A full settings save always includes lab_id/api_key so this
        never showed up in practice, but any partial save (e.g. just the
        samples folder) would silently blank the live poller's credentials."""
        client.post("/api/save_settings",
                    data=json.dumps({"lab_id": "LAB1", "api_key": "KEY1"}), content_type="application/json")
        assert client.application.test_poller.lab_id == "LAB1"

        client.post("/api/save_settings",
                    data=json.dumps({"samples_folder": "/Users/studio/Samples"}), content_type="application/json")
        assert client.application.test_poller.lab_id == "LAB1"
        assert client.application.test_poller.api_key == "KEY1"

    def test_save_then_get_roundtrip(self, client):
        payload = {
            "lab_id": "lab123",
            "poll_interval": 30,
            "studio_name": "Wayne's Photos",
        }
        client.post("/api/save_settings",
                    data=json.dumps(payload), content_type="application/json")
        data = client.get("/api/get_settings").get_json()
        assert data["lab_id"] == "lab123"
        assert data["poll_interval"] == 30

    def test_get_printers_returns_list(self, client):
        resp = client.get("/api/get_printers")
        assert resp.status_code == 200
        assert isinstance(resp.get_json(), list)

    def test_test_connection_no_credentials(self, client):
        resp = client.post("/api/test_connection",
                           data=json.dumps({"lab_id": "", "api_key": ""}),
                           content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


# ── Primary destination naming ─────────────────────────────────────────────────

class TestSetPrimaryDestinationName:
    def test_seeds_and_names_when_none_exist(self, client):
        config.save({"image_output_folder": "C:\\Hot"})
        resp = client.post("/api/set_primary_destination_name",
                           data=json.dumps({"name": "Front Desk DNP"}),
                           content_type="application/json")
        result = resp.get_json()
        assert result["ok"] is True
        assert result["name"] == "Front Desk DNP"
        assert db.get_destinations()[0]["name"] == "Front Desk DNP"

    def test_renames_existing_destination(self, client):
        db.upsert_destination("Old Name", "C:\\Hot", is_default=True)
        resp = client.post("/api/set_primary_destination_name",
                           data=json.dumps({"name": "New Name"}),
                           content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert db.get_destinations()[0]["name"] == "New Name"

    def test_blank_name_defaults_to_printer_1(self, client):
        config.save({"image_output_folder": "C:\\Hot"})
        resp = client.post("/api/set_primary_destination_name",
                           data=json.dumps({"name": ""}),
                           content_type="application/json")
        assert resp.get_json()["name"] == "Printer 1"
        assert db.get_destinations()[0]["name"] == "Printer 1"


# ── Logo upload ────────────────────────────────────────────────────────────────

class TestUploadLogo:
    def test_accepts_small_file(self, client, tmp_path, monkeypatch):
        # upload_logo() saves via config._app_dir() (a plain filesystem path, not
        # CONFIG_PATH) — must isolate it too or the test writes into the real repo.
        import config as _config
        monkeypatch.setattr(_config, "_app_dir", lambda: str(tmp_path))

        data = {"file": (io.BytesIO(b"\x89PNG\r\n" + b"x" * 100), "logo.png")}
        resp = client.post("/api/upload_logo", data=data, content_type="multipart/form-data")
        assert resp.get_json()["ok"] is True
        assert (tmp_path / "studio_logo.png").exists()

    def test_rejects_oversized_file(self, client):
        import server
        oversized = b"x" * (server.MAX_LOGO_SIZE_BYTES + 1)
        data = {"file": (io.BytesIO(oversized), "logo.png")}
        resp = client.post("/api/upload_logo", data=data, content_type="multipart/form-data")
        result = resp.get_json()
        assert result["ok"] is False
        assert "5MB" in result["error"]

    def test_rejects_unsupported_extension(self, client):
        data = {"file": (io.BytesIO(b"not an image"), "logo.gif")}
        resp = client.post("/api/upload_logo", data=data, content_type="multipart/form-data")
        assert resp.get_json()["ok"] is False


# ── Queue ─────────────────────────────────────────────────────────────────────

class TestQueue:
    def test_empty_queue(self, client):
        assert client.get("/api/get_queue").get_json() == []

    def test_queue_returns_inserted_order(self, client):
        db.upsert_order(self._make_order("ORD001", "Gallery A"))
        resp = client.get("/api/get_queue").get_json()
        assert len(resp) == 1
        assert resp[0]["order_num"] == "ORD001"

    def test_queue_gallery_filter(self, client):
        db.upsert_order(self._make_order("ORD001", "Gallery A"))
        db.upsert_order(self._make_order("ORD002", "Gallery B"))

        resp_a = client.get("/api/get_queue?gallery=Gallery+A").get_json()
        assert len(resp_a) == 1
        assert resp_a[0]["order_num"] == "ORD001"

        resp_b = client.get("/api/get_queue?gallery=Gallery+B").get_json()
        assert len(resp_b) == 1

    def test_queue_empty_gallery_param_returns_all(self, client):
        db.upsert_order(self._make_order("ORD001", "Gallery A"))
        db.upsert_order(self._make_order("ORD002", "Gallery B"))
        assert len(client.get("/api/get_queue?gallery=").get_json()) == 2

    @staticmethod
    def _make_order(num, gallery):
        return {
            "num": num, "gallery": gallery, "status": "received",
            "placedAt": "2026-01-01T00:00:00Z", "items": [],
            "shipping": {"option": {"externalId": "pdx_pickup"},
                         "destination": {"recipient": "Test Customer"}},
        }


# ── History ───────────────────────────────────────────────────────────────────

class TestHistory:
    def test_empty_history(self, client):
        assert client.get("/api/get_history").get_json() == []

    def test_history_appears_after_confirm(self, client):
        db.upsert_order({"num": "ORD001", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "pdx_pickup"},
                                      "destination": {"recipient": "C"}}})
        db.confirm_order("ORD001")
        history = client.get("/api/get_history").get_json()
        assert len(history) == 1
        assert history[0]["order_num"] == "ORD001"

    def test_history_gallery_filter(self, client):
        for num, gallery in [("ORD001", "GA"), ("ORD002", "GB")]:
            db.upsert_order({"num": num, "gallery": gallery, "status": "received",
                             "placedAt": "2026-01-01T00:00:00Z", "items": [],
                             "shipping": {"option": {"externalId": "pdx_pickup"},
                                          "destination": {"recipient": "C"}}})
            db.confirm_order(num)

        resp = client.get("/api/get_history?gallery=GA").get_json()
        assert len(resp) == 1
        assert resp[0]["gallery"] == "GA"


# ── Stats ─────────────────────────────────────────────────────────────────────

class TestStats:
    def test_empty_stats(self, client):
        data = client.get("/api/get_stats").get_json()
        assert data["total"] == 0
        assert data["pending"] == 0
        assert data["confirmed"] == 0

    def test_stats_reflect_inserted_orders(self, client):
        for i in range(3):
            db.upsert_order({"num": f"ORD{i}", "gallery": "G", "status": "received",
                             "placedAt": "2026-01-01T00:00:00Z", "items": [],
                             "shipping": {"option": {"externalId": "pdx_pickup"},
                                          "destination": {"recipient": "C"}}})
        data = client.get("/api/get_stats").get_json()
        assert data["total"] == 3
        assert data["pending"] == 3

    def test_stats_gallery_filter(self, client):
        for i in range(2):
            db.upsert_order({"num": f"GA{i}", "gallery": "Gallery A", "status": "received",
                             "placedAt": "2026-01-01T00:00:00Z", "items": [],
                             "shipping": {"option": {"externalId": "pdx_pickup"},
                                          "destination": {"recipient": "C"}}})
        db.upsert_order({"num": "GB0", "gallery": "Gallery B", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "pdx_pickup"},
                                      "destination": {"recipient": "C"}}})

        stats_a = client.get("/api/get_stats?gallery=Gallery+A").get_json()
        assert stats_a["total"] == 2

        stats_all = client.get("/api/get_stats").get_json()
        assert stats_all["total"] == 3


# ── Order lookup / search ─────────────────────────────────────────────────────

class TestOrderLookup:
    def test_get_order_not_found(self, client):
        assert client.get("/api/get_order?order_num=NOTEXIST").status_code == 404

    def test_get_order_found(self, client):
        db.upsert_order({"num": "ORD001", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "pdx_pickup"},
                                      "destination": {"recipient": "Jane Doe"}}})
        data = client.get("/api/get_order?order_num=ORD001").get_json()
        assert data["order_num"] == "ORD001"
        assert data["customer_name"] == "Jane Doe"

    def test_search_empty_query_returns_empty(self, client):
        assert client.get("/api/search?q=").get_json() == []

    def test_search_finds_by_order_num(self, client):
        db.upsert_order({"num": "FINDME001", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "pdx_pickup"},
                                      "destination": {"recipient": "C"}}})
        results = client.get("/api/search?q=FINDME001").get_json()
        assert len(results) == 1

    def test_get_galleries_empty(self, client):
        assert client.get("/api/get_galleries").get_json() == []

    def test_get_galleries_after_insert(self, client):
        db.upsert_order({"num": "ORD001", "gallery": "My Gallery", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "pdx_pickup"},
                                      "destination": {"recipient": "C"}}})
        galleries = client.get("/api/get_galleries").get_json()
        assert "My Gallery" in galleries

    def test_get_jobs_empty(self, client):
        assert client.get("/api/get_jobs").get_json() == []

    def test_get_jobs_after_insert(self, client):
        db.upsert_order({"num": "ORD001", "gallery": "Job Gallery", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "pdx_pickup"},
                                      "destination": {"recipient": "C"}}})
        jobs = client.get("/api/get_jobs").get_json()
        assert any(j["gallery"] == "Job Gallery" for j in jobs)


class TestGetProductsForJob:
    def test_no_gallery_returns_empty(self, client):
        assert client.get("/api/get_products_for_job").get_json() == []

    def test_returns_products_scoped_to_gallery(self, client):
        dest_id = db.upsert_destination("A", "C:\\A")
        db.upsert_order({"num": "ORD001", "gallery": "Job A", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "pdx_pickup"}, "destination": {"recipient": "C"}}})
        order = db.get_order("ORD001")
        db.insert_order_item(order["id"], "a.jpg", "8x24", dest_id)
        specs = client.get("/api/get_products_for_job?gallery=Job A").get_json()
        assert specs == ["8x24"]
        assert client.get("/api/get_products_for_job?gallery=Job B").get_json() == []


class TestDiscoverSpecsEndpoint:
    def test_captures_item_description_alongside_spec(self, client, monkeypatch):
        import api as pdx_api
        config.save({"lab_id": "L1", "api_key": "K1"})
        monkeypatch.setattr(pdx_api, "poll_orders", lambda lab_id, api_key: ([
            {"items": [{"description": "2 Poster COMBO",
                        "images": [{"externalId": "combo2_8x24"}]}]}
        ], None))
        resp = client.post("/api/discover_specs", data="{}", content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["added"] == 1
        routing = client.get("/api/get_routing").get_json()
        assert routing[0]["print_spec"] == "combo2_8x24"
        assert routing[0]["description"] == "2 Poster COMBO"

    def test_real_order_sample_YH1785181240(self, client, monkeypatch):
        """Regression test using a real PDX order export (YH1785181240,
        Treasure Island Baseball) — confirms we key off the per-IMAGE externalId
        (the actual print_spec/routing key, e.g. "adfadfasdf" for the 8x10 item —
        yes, that's really what PDX sent) and pair it with the ITEM-level
        description (e.g. "8x10"), not the item's own externalId ("8x10-1")."""
        import api as pdx_api
        config.save({"lab_id": "L1", "api_key": "K1"})
        real_order = {"items": [
            {"externalId": "2x3keychain", "description": "2x3 Keychain",
             "images": [{"externalId": "2x3", "filename": "Treasure Island Day 2-773-97389f26.jpg"}]},
            {"externalId": "5x7", "description": "5x7",
             "images": [{"externalId": "5x7", "filename": "Treasure Island Day 2-525-158d5a7c.jpg"}]},
            {"externalId": "8x10-1", "description": "8x10",
             "images": [{"externalId": "adfadfasdf", "filename": "Treasure Island Day 2-549-cf0d886f.jpg"}]},
        ]}
        monkeypatch.setattr(pdx_api, "poll_orders", lambda lab_id, api_key: ([real_order], None))
        resp = client.post("/api/discover_specs", data="{}", content_type="application/json")
        assert resp.get_json()["added"] == 3
        routing = {r["print_spec"]: r["description"] for r in client.get("/api/get_routing").get_json()}
        assert routing == {"2x3": "2x3 Keychain", "5x7": "5x7", "adfadfasdf": "8x10"}


class TestHistoricalBackfillIngestsAllModes:
    """Regression test: poller._order_matches_mode was deleted in Phase 3 (order
    ingestion became mode-agnostic — see db.upsert_order), but two historical-
    backfill call sites in server.py kept importing it, silently failing every
    time (job dropdown clicks / every Settings save) instead of backfilling any
    orders. Both paths must now ingest every returned order unconditionally,
    regardless of the (now otherwise-unused) global fulfillment_mode setting."""

    class _SyncThread:
        """Runs the thread's target synchronously so the test can assert
        immediately instead of racing a real background thread."""
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}
        def start(self):
            self._target(*self._args, **self._kwargs)

    def test_fetch_job_history_ingests_regardless_of_fulfillment_mode(
        self, client, app, monkeypatch, pickup_order, dropship_order
    ):
        import server as _server
        import api as pdx_api

        monkeypatch.setattr(threading, "Thread", self._SyncThread)
        monkeypatch.setattr(
            pdx_api, "fetch_all_orders_for_job",
            lambda lab_id, api_key, gallery: ([pickup_order, dropship_order], None)
        )
        config.save({"lab_id": "L1", "api_key": "K1"})

        resp = client.post("/api/fetch_job_history",
                           data=json.dumps({"gallery": pickup_order["gallery"]}),
                           content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert db.get_order(pickup_order["num"]) is not None
        assert db.get_order(dropship_order["num"]) is not None

    def test_seed_jobs_background_ingests_regardless_of_fulfillment_mode(
        self, app, monkeypatch, pickup_order, dropship_order
    ):
        import api as pdx_api
        from server import _seed_jobs_background

        monkeypatch.setattr(pdx_api, "fetch_historical_orders",
                           lambda lab_id, api_key, limit_per_status=100: ([pickup_order, dropship_order], None))
        monkeypatch.setattr(db, "migrate_fulfilled_orders", lambda *a, **k: None)
        config.save({"lab_id": "L1", "api_key": "K1"})

        _seed_jobs_background("L1", "K1")  # should not raise (was throwing ImportError)
        assert db.get_order(pickup_order["num"]) is not None
        assert db.get_order(dropship_order["num"]) is not None


# ── Order actions (no credentials — returns graceful failures) ────────────────

class TestOrderActions:
    def test_confirm_order_no_credentials(self, client):
        resp = client.post("/api/confirm_order",
                           data=json.dumps({"order_num": "ORD001"}),
                           content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert "error" in data

    def test_confirm_order_tags_activity_log_with_station_name(self, client, monkeypatch):
        import api as pdx_api
        db.upsert_order({"num": "ORD001", "gallery": "Test Job",
                         "shipping": {"option": {"externalId": "pdx_pickup"}, "destination": {}}})
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))

        client.post("/api/confirm_order", data=json.dumps({"order_num": "ORD001"}),
                   content_type="application/json", headers={"X-Station-Name": "Check-in 2"})

        log = client.get("/api/activity_log").get_json()
        assert any("ORD001" in e["message"] and "Check-in 2" in e["message"] for e in log)

    def test_confirm_order_without_station_header_has_no_tag(self, client, monkeypatch):
        import api as pdx_api
        db.upsert_order({"num": "ORD002", "gallery": "Test Job",
                         "shipping": {"option": {"externalId": "pdx_pickup"}, "destination": {}}})
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))

        client.post("/api/confirm_order", data=json.dumps({"order_num": "ORD002"}), content_type="application/json")

        log = client.get("/api/activity_log").get_json()
        entry = next(e for e in log if "ORD002" in e["message"])
        assert entry["message"] == "✅ Confirmed (scanned): ORD002"

    def test_success_records_shipped_notification(self, client, monkeypatch):
        import api as pdx_api
        db.upsert_order({"num": "ORD003", "gallery": "Test Job",
                         "shipping": {"option": {"externalId": "pdx_pickup"}, "destination": {}}})
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))

        resp = client.post("/api/confirm_order", data=json.dumps({"order_num": "ORD003"}), content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert db.has_shipped_notification("ORD003") is True

    def test_double_scan_returns_clean_already_shipped_error(self, client, monkeypatch):
        """Regression test: confirm_order had no dedup check at all, unlike
        Mark Shipped/Ready to Ship — an eager double-scan of the same QR code
        would hit whatever raw error PDX's own rejection happened to say."""
        import api as pdx_api
        db.upsert_order({"num": "ORD004", "gallery": "Test Job",
                         "shipping": {"option": {"externalId": "pdx_pickup"}, "destination": {}}})
        calls = []
        monkeypatch.setattr(pdx_api, "shipped_callback",
                           lambda *a, **k: (calls.append(1), (True, ""))[1])

        first = client.post("/api/confirm_order", data=json.dumps({"order_num": "ORD004"}), content_type="application/json")
        second = client.post("/api/confirm_order", data=json.dumps({"order_num": "ORD004"}), content_type="application/json")

        assert first.get_json()["ok"] is True
        data = second.get_json()
        assert data["ok"] is False
        assert data["error"] == "Order already marked shipped"
        assert len(calls) == 1  # the second scan never even hit PDX

    def test_pdx_already_shipped_rejection_is_treated_as_success(self, client, monkeypatch):
        """Covers the case a local dedup check can't: PDX already knows the
        order shipped (e.g. another station confirmed it) even though this
        station's own shipped_notifications table doesn't have a record yet."""
        import api as pdx_api
        db.upsert_order({"num": "ORD005", "gallery": "Test Job",
                         "shipping": {"option": {"externalId": "pdx_pickup"}, "destination": {}}})
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (False, "Order already shipped"))

        resp = client.post("/api/confirm_order", data=json.dumps({"order_num": "ORD005"}), content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert db.get_order("ORD005")["status"] == "fulfilled"
        assert db.has_shipped_notification("ORD005") is True

    def test_fulfill_order_no_images(self, client):
        resp = client.post("/api/fulfill_order",
                           data=json.dumps({"order_num": "NOTEXIST"}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is False

    def test_reprint_receipt_not_found(self, client):
        resp = client.post("/api/reprint_receipt",
                           data=json.dumps({"order_num": "NOTEXIST"}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is False


class TestMarkShipped:
    def test_rejects_invalid_carrier(self, client):
        resp = client.post("/api/mark_shipped",
                           data=json.dumps({"order_num": "ORD001", "carrier": "CARRIER_PIGEON", "tracking_number": "123"}),
                           content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert "Invalid carrier" in data["error"]

    def test_requires_tracking_number_for_non_pickup_carrier(self, client):
        resp = client.post("/api/mark_shipped",
                           data=json.dumps({"order_num": "ORD001", "carrier": "UPS", "tracking_number": ""}),
                           content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert "Tracking number" in data["error"]

    def test_success_confirms_order_and_calls_pdx(self, client, monkeypatch):
        import api as pdx_api
        db.upsert_order({"num": "SHIP001", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "economy"},
                                      "destination": {"recipient": "C"}}})
        calls = []
        monkeypatch.setattr(pdx_api, "shipped_callback",
                           lambda lab_id, api_key, order_num, carrier="Pickup", tracking_number="":
                               (calls.append((order_num, carrier, tracking_number)), (True, ""))[1])

        resp = client.post("/api/mark_shipped",
                           data=json.dumps({"order_num": "SHIP001", "carrier": "ups", "tracking_number": "1Z999"}),
                           content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert calls == [("SHIP001", "UPS", "1Z999")]
        assert db.get_order("SHIP001")["status"] == "fulfilled"

    def test_hand_delivered_does_not_require_tracking_number(self, client, monkeypatch):
        import api as pdx_api
        db.upsert_order({"num": "SHIP003", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "economy-bulk"},
                                      "destination": {"recipient": "C"}}})
        calls = []
        monkeypatch.setattr(pdx_api, "shipped_callback",
                           lambda lab_id, api_key, order_num, carrier="Pickup", tracking_number="":
                               (calls.append((order_num, carrier, tracking_number)), (True, ""))[1])

        resp = client.post("/api/mark_shipped",
                           data=json.dumps({"order_num": "SHIP003", "carrier": "hand_delivered", "tracking_number": ""}),
                           content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert calls == [("SHIP003", "HAND_DELIVERED", "")]
        assert db.get_order("SHIP003")["status"] == "fulfilled"

    def test_pdx_failure_does_not_confirm_order(self, client, monkeypatch):
        import api as pdx_api
        db.upsert_order({"num": "SHIP002", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "economy"},
                                      "destination": {"recipient": "C"}}})
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (False, "bad api key"))

        resp = client.post("/api/mark_shipped",
                           data=json.dumps({"order_num": "SHIP002", "carrier": "FEDEX", "tracking_number": "999"}),
                           content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"] == "bad api key"
        assert db.get_order("SHIP002")["status"] == "received"

    def test_records_shipped_notification_for_dedup(self, client, monkeypatch):
        """Regression test for a real bug found by code review: mark_shipped
        confirmed the order but never wrote to shipped_notifications, so the
        dedup log this session built specifically to prevent double-shipping
        across paths (manual Mark Shipped vs. automated Ready to Ship) only
        ever covered half of it."""
        import api as pdx_api
        db.upsert_order({"num": "SHIP003", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "economy"}, "destination": {"recipient": "C"}}})
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))

        client.post("/api/mark_shipped",
                   data=json.dumps({"order_num": "SHIP003", "carrier": "UPS", "tracking_number": "1Z1"}),
                   content_type="application/json")
        assert db.has_shipped_notification("SHIP003") is True

    def test_rejects_an_order_already_shipped(self, client):
        db.upsert_order({"num": "SHIP004", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "economy"}, "destination": {"recipient": "C"}}})
        db.record_shipped_notification("SHIP004", "UPS", "1Z1", "manual")

        resp = client.post("/api/mark_shipped",
                           data=json.dumps({"order_num": "SHIP004", "carrier": "FEDEX", "tracking_number": "999"}),
                           content_type="application/json")
        assert resp.get_json()["ok"] is False

    def test_prevents_ready_to_ship_from_double_shipping_after_manual_mark(self, client, monkeypatch):
        """The real end-to-end cross-path guarantee: an order manually marked
        shipped through the actual /api/mark_shipped endpoint (not a direct
        db insert) must block Ready to Ship from also buying a real label
        for it — this is the exact scenario the shipped_notifications table
        exists to prevent, and it silently didn't work until this fix."""
        import api as pdx_api
        import shipping_providers as sp
        db.upsert_order({"num": "SHIP005", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "pdx_economy", "name": "Economy"},
                                     "destination": {"recipient": "Jane Doe", "address1": "123 Main St",
                                                    "city": "Orlando", "state": "FL", "zipCode": "32789"}}})
        pid = db.upsert_shipping_provider("shipstation", "SS", {"api_key": "k", "api_secret": "s"})
        db.upsert_shipping_option_mapping(pid, "pdx_economy", "Economy", "stamps_com", "usps_priority_mail", "", "none", "USPS")
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))
        create_label_calls = []
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label",
                           lambda self, *a, **k: (create_label_calls.append(1), ({"tracking_number": "X"}, ""))[1])

        client.post("/api/mark_shipped",
                   data=json.dumps({"order_num": "SHIP005", "carrier": "UPS", "tracking_number": "1Z1"}),
                   content_type="application/json")

        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "SHIP005"}), content_type="application/json")
        assert resp.get_json()["ok"] is False
        assert not create_label_calls  # no real label was purchased for the second path


class TestReprintImagesResetsStatus:
    """Reprinting an item must reset its order_items status back to 'queued' so
    Poller._check_pending_prints() can re-detect it printing once it disappears
    from the hot folder again — otherwise a reprinted item stays stuck showing
    its old status (e.g. 'error' or 'printed') forever."""

    def _setup_order(self, pickup_order):
        import printer as _printer
        db.upsert_order(pickup_order)
        order = db.get_order(pickup_order["num"])
        dest_id = db.upsert_destination("A", "C:\\A")
        filename = pickup_order["items"][0]["images"][0]["filename"]
        item_id = db.insert_order_item(order["id"], filename, "8x24", dest_id)
        db.update_item_status(item_id, "error")
        return order, item_id, filename

    def test_reprint_resets_item_to_queued(self, client, app, pickup_order, monkeypatch):
        order, item_id, filename = self._setup_order(pickup_order)
        import printer as _printer
        monkeypatch.setattr(_printer, "reprint_images_to_hot_folder", lambda *a, **k: (True, ""))

        resp = client.post("/api/reprint_images",
                           data=json.dumps({"order_num": order["order_num"]}),
                           content_type="application/json")
        assert resp.get_json()["ok"] is True

        items = db.get_order_items(order["order_num"])
        assert items[0]["status"] == "queued"
        assert items[0]["printed_at"] is None
        # And it's eligible for the hot-folder-consumption check again
        assert any(p["filename"] == filename for p in db.get_pending_order_items())

    def test_reprint_resets_only_selected_filenames(self, client, app, pickup_order, monkeypatch):
        order, item_id, filename = self._setup_order(pickup_order)
        dest_id = db.upsert_destination("B", "C:\\B")
        other_item_id = db.insert_order_item(order["id"], "other.jpg", "5x7", dest_id)
        db.update_item_status(other_item_id, "printed")
        # get_images_json only returns what's in images_json (one image for this fixture),
        # so simulate a second image being present on the order too
        with db.get_conn() as conn:
            images = json.loads(conn.execute(
                "SELECT images_json FROM orders WHERE id=?", (order["id"],)
            ).fetchone()[0])
            images.append({"filename": "other.jpg", "item_sku": "5x7", "item_idx": 1})
            conn.execute("UPDATE orders SET images_json=? WHERE id=?", (json.dumps(images), order["id"]))
            conn.commit()

        import printer as _printer
        monkeypatch.setattr(_printer, "reprint_images_to_hot_folder", lambda *a, **k: (True, ""))

        resp = client.post("/api/reprint_images",
                           data=json.dumps({"order_num": order["order_num"], "filenames": [filename]}),
                           content_type="application/json")
        assert resp.get_json()["ok"] is True

        items_by_file = {it["filename"]: it for it in db.get_order_items(order["order_num"])}
        assert items_by_file[filename]["status"] == "queued"
        assert items_by_file["other.jpg"]["status"] == "printed"  # untouched — wasn't selected

    def test_reprint_via_redownload_fallback_also_resets_status(self, client, app, pickup_order, monkeypatch):
        order, item_id, filename = self._setup_order(pickup_order)
        import printer as _printer
        monkeypatch.setattr(_printer, "reprint_images_to_hot_folder", lambda *a, **k: (False, "archive missing"))
        monkeypatch.setattr(_printer, "download_images", lambda *a, **k: (True, ""))

        resp = client.post("/api/reprint_images",
                           data=json.dumps({"order_num": order["order_num"]}),
                           content_type="application/json")
        assert resp.get_json()["ok"] is True
        items = db.get_order_items(order["order_num"])
        assert items[0]["status"] == "queued"

    def test_reprint_failure_does_not_reset_status(self, client, app, pickup_order, monkeypatch):
        order, item_id, filename = self._setup_order(pickup_order)
        import printer as _printer
        monkeypatch.setattr(_printer, "reprint_images_to_hot_folder", lambda *a, **k: (False, "archive missing"))
        monkeypatch.setattr(_printer, "download_images", lambda *a, **k: (False, "api down"))

        resp = client.post("/api/reprint_images",
                           data=json.dumps({"order_num": order["order_num"]}),
                           content_type="application/json")
        assert resp.get_json()["ok"] is False
        items = db.get_order_items(order["order_num"])
        assert items[0]["status"] == "error"  # unchanged since reprint never actually succeeded


# ── Packing slip (in-studio) ────────────────────────────────────────────────────

class TestPackingSlip:
    def test_packing_slip_pdf_no_orders_specified(self, client):
        resp = client.post("/api/packing_slip_pdf",
                           data=json.dumps({"order_nums": []}),
                           content_type="application/json")
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_packing_slip_pdf_order_not_found(self, client):
        resp = client.post("/api/packing_slip_pdf",
                           data=json.dumps({"order_nums": ["NOTEXIST"]}),
                           content_type="application/json")
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_packing_slip_pdf_returns_real_pdf(self, client, pickup_order):
        db.upsert_order(pickup_order)
        resp = client.post("/api/packing_slip_pdf",
                           data=json.dumps({"order_nums": [pickup_order["num"]]}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.content_type == "application/pdf"
        assert resp.data[:4] == b"%PDF"

    def test_packing_slip_pdf_passes_image_output_folder_for_dropship_thumbnails(self, client, pickup_order, monkeypatch):
        # Bulk orders are commonly dropship-classified — their thumbnails live in
        # dropship/ORDER_NUM/, resolved via image_output_folder. Confirm the
        # endpoint actually threads it through rather than leaving it blank.
        import printer
        db.upsert_order(pickup_order)
        config.save({"image_output_folder": "C:\\Hot\\Folder"})
        captured = {}
        real_build = printer.build_packing_slips_pdf
        def spy(orders, destinations, studio_name="", image_output_folder=""):
            captured["image_output_folder"] = image_output_folder
            return real_build(orders, destinations, studio_name, image_output_folder)
        monkeypatch.setattr(printer, "build_packing_slips_pdf", spy)

        client.post("/api/packing_slip_pdf",
                   data=json.dumps({"order_nums": [pickup_order["num"]]}),
                   content_type="application/json")
        assert captured["image_output_folder"] == "C:\\Hot\\Folder"

    def test_packing_slip_pdf_combines_multiple_orders(self, client, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)
        one = client.post("/api/packing_slip_pdf",
                          data=json.dumps({"order_nums": [pickup_order["num"]]}),
                          content_type="application/json").data
        two = client.post("/api/packing_slip_pdf",
                          data=json.dumps({"order_nums": [pickup_order["num"], dropship_order["num"]]}),
                          content_type="application/json").data
        assert len(two) > len(one)

    def test_packing_slip_pdf_skips_missing_orders_in_a_batch(self, client, pickup_order):
        db.upsert_order(pickup_order)
        resp = client.post("/api/packing_slip_pdf",
                           data=json.dumps({"order_nums": [pickup_order["num"], "NOTEXIST"]}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.data[:4] == b"%PDF"

    def test_mark_slips_printed_unknown_order_not_marked(self, client):
        resp = client.post("/api/mark_slips_printed",
                           data=json.dumps({"order_nums": ["NOTEXIST"]}),
                           content_type="application/json")
        result = resp.get_json()
        assert result["ok"] is True
        assert result["marked"] == []

    def test_mark_slips_printed_single_order(self, client, pickup_order):
        db.upsert_order(pickup_order)
        resp = client.post("/api/mark_slips_printed",
                           data=json.dumps({"order_nums": [pickup_order["num"]]}),
                           content_type="application/json")
        result = resp.get_json()
        assert result["ok"] is True
        assert result["marked"] == [pickup_order["num"]]
        assert db.get_order(pickup_order["num"])["fulfill_status"] == "fulfilled"

    def test_mark_slips_printed_batch(self, client, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)
        resp = client.post("/api/mark_slips_printed",
                           data=json.dumps({"order_nums": [pickup_order["num"], dropship_order["num"]]}),
                           content_type="application/json")
        result = resp.get_json()
        assert set(result["marked"]) == {pickup_order["num"], dropship_order["num"]}
        assert db.get_order(pickup_order["num"])["fulfill_status"] == "fulfilled"
        assert db.get_order(dropship_order["num"])["fulfill_status"] == "fulfilled"

    def test_mark_slips_printed_empty_list(self, client):
        resp = client.post("/api/mark_slips_printed",
                           data=json.dumps({"order_nums": []}),
                           content_type="application/json")
        assert resp.get_json() == {"ok": True, "marked": []}


# ── Poller / system routes ────────────────────────────────────────────────────

class TestSystem:
    def test_get_poller_status(self, client):
        data = client.get("/api/get_poller_status").get_json()
        assert "running" in data
        assert "next_poll_in" in data
        assert "interval" in data

    def test_trigger_poll(self, client):
        resp = client.post("/api/trigger_poll")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_get_version(self, client):
        import updater
        data = client.get("/api/get_version").get_json()
        assert data["version"] == updater.APP_VERSION

    def test_get_pending_update_default(self, client):
        data = client.get("/api/get_pending_update").get_json()
        assert data["update_available"] is False

    def test_activity_log_empty(self, client):
        assert isinstance(client.get("/api/activity_log").get_json(), list)

    def test_activity_log_write_and_read(self, client):
        client.post("/api/activity_log_write",
                    data=json.dumps({"message": "Test entry", "level": "info"}),
                    content_type="application/json")
        log = client.get("/api/activity_log").get_json()
        assert any(entry["message"] == "Test entry" for entry in log)

    def test_export_logs_returns_zip_with_activity_log(self, client):
        import io
        import zipfile

        client.post("/api/activity_log_write",
                    data=json.dumps({"message": "Export me", "level": "info"}),
                    content_type="application/json")
        resp = client.get("/api/export_logs")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"
        assert "attachment" in resp.headers.get("Content-Disposition", "")

        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        assert set(zf.namelist()) == {"activity_log.txt", "pdx_onsite.log"}
        assert "Export me" in zf.read("activity_log.txt").decode("utf-8")

    def test_export_logs_activity_log_not_capped_at_50(self, client):
        for i in range(60):
            client.post("/api/activity_log_write",
                        data=json.dumps({"message": f"Entry {i}", "level": "info"}),
                        content_type="application/json")
        import io
        import zipfile
        resp = client.get("/api/export_logs")
        zf = zipfile.ZipFile(io.BytesIO(resp.data))
        text = zf.read("activity_log.txt").decode("utf-8")
        assert "Entry 0" in text  # the live panel's limit=50 would have dropped this

    def test_sse_endpoint_content_type(self, client):
        with client.get("/api/events") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.content_type

    def test_sse_broadcasts_to_every_connected_client(self, client):
        """Regression test for the single-shared-Queue bug: with two stations
        connected (same-location multi-station workflow), an event used to go
        to whichever client's generator happened to dequeue it first, not
        both. Each client must now get its own copy of every event."""
        import server

        with client.get("/api/events") as resp1, client.get("/api/events") as resp2:
            iter1, iter2 = resp1.iter_encoded(), resp2.iter_encoded()
            next(iter1)  # "connected" handshake
            next(iter2)

            server.push_event("broadcast_test", {"n": 1})

            chunk1 = next(iter1)
            chunk2 = next(iter2)
            assert b"broadcast_test" in chunk1
            assert b"broadcast_test" in chunk2

    def test_samples_list_no_folder(self, client):
        data = client.get("/api/samples/list").get_json()
        assert "files" in data
        assert data["files"] == []

    def test_image_not_found(self, client):
        resp = client.get("/api/image/ORD001/fake.jpg")
        assert resp.status_code in (404, 404)


class TestShippingProviderEndpoints:
    def test_catalog_includes_shipstation(self, client):
        catalog = client.get("/api/get_shipping_provider_catalog").get_json()
        assert any(p["provider_type"] == "shipstation" for p in catalog)

    def test_empty_providers_list(self, client):
        assert client.get("/api/get_shipping_providers").get_json() == []

    def test_save_and_list_provider(self, client):
        resp = client.post("/api/save_shipping_provider", data=json.dumps({
            "provider_type": "shipstation", "label": "Bassetti ShipStation",
            "credentials": {"api_key": "k", "api_secret": "s"}, "enabled": True,
        }), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is True
        providers = client.get("/api/get_shipping_providers").get_json()
        assert len(providers) == 1
        assert providers[0]["label"] == "Bassetti ShipStation"

    def test_save_rejects_unknown_provider_type(self, client):
        resp = client.post("/api/save_shipping_provider", data=json.dumps({
            "provider_type": "not_real", "label": "X", "credentials": {},
        }), content_type="application/json")
        assert resp.get_json()["ok"] is False

    def test_update_existing_provider(self, client):
        pid = client.post("/api/save_shipping_provider", data=json.dumps({
            "provider_type": "shipstation", "label": "SS", "credentials": {"api_key": "k1", "api_secret": "s1"},
        }), content_type="application/json").get_json()["id"]
        client.post("/api/save_shipping_provider", data=json.dumps({
            "id": pid, "provider_type": "shipstation", "label": "SS Renamed",
            "credentials": {"api_key": "k2", "api_secret": "s2"},
        }), content_type="application/json")
        providers = client.get("/api/get_shipping_providers").get_json()
        assert len(providers) == 1
        assert providers[0]["label"] == "SS Renamed"

    def test_delete_provider(self, client):
        pid = client.post("/api/save_shipping_provider", data=json.dumps({
            "provider_type": "shipstation", "label": "SS", "credentials": {},
        }), content_type="application/json").get_json()["id"]
        resp = client.post("/api/delete_shipping_provider", data=json.dumps({"id": pid}), content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert client.get("/api/get_shipping_providers").get_json() == []

    def _make_provider(self, client):
        return client.post("/api/save_shipping_provider", data=json.dumps({
            "provider_type": "shipstation", "label": "SS", "credentials": {"api_key": "k", "api_secret": "s"},
        }), content_type="application/json").get_json()["id"]

    def test_list_provider_carriers(self, client, monkeypatch):
        import shipping_providers as sp
        pid = self._make_provider(client)
        monkeypatch.setattr(sp.ShipStationV1Adapter, "list_carriers",
                           lambda self: ([{"code": "ups", "name": "UPS"}], ""))
        resp = client.get(f"/api/list_provider_carriers?provider_id={pid}")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["carriers"] == [{"code": "ups", "name": "UPS"}]

    def test_list_provider_carriers_not_found(self, client):
        resp = client.get("/api/list_provider_carriers?provider_id=999")
        assert resp.get_json()["ok"] is False

    def test_list_provider_services(self, client, monkeypatch):
        import shipping_providers as sp
        pid = self._make_provider(client)
        monkeypatch.setattr(sp.ShipStationV1Adapter, "list_services",
                           lambda self, carrier_code: ([{"code": "ups_ground", "name": "UPS Ground"}], ""))
        resp = client.get(f"/api/list_provider_services?provider_id={pid}&carrier_code=ups")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["services"] == [{"code": "ups_ground", "name": "UPS Ground"}]

    def test_list_provider_services_requires_carrier_code(self, client):
        pid = self._make_provider(client)
        resp = client.get(f"/api/list_provider_services?provider_id={pid}")
        assert resp.get_json()["ok"] is False

    def test_list_provider_packages(self, client, monkeypatch):
        import shipping_providers as sp
        pid = self._make_provider(client)
        monkeypatch.setattr(sp.ShipStationV1Adapter, "list_packages",
                           lambda self, carrier_code: ([{"code": "large_flat_rate_box", "name": "Large Flat Rate Box"}], ""))
        resp = client.get(f"/api/list_provider_packages?provider_id={pid}&carrier_code=stamps_com")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["packages"][0]["code"] == "large_flat_rate_box"

    def test_get_known_shipping_options(self, client):
        db.upsert_order({"num": "ORD001", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "pdx_economy", "name": "Economy"},
                                      "destination": {"recipient": "C"}}})
        options = client.get("/api/get_known_shipping_options").get_json()
        assert {"external_id": "pdx_economy", "name": "Economy"} in options

    def test_save_and_get_shipping_option_mapping(self, client):
        pid = self._make_provider(client)
        resp = client.post("/api/save_shipping_option_mapping", data=json.dumps({
            "provider_id": pid, "pdx_option_external_id": "pdx_economy", "pdx_option_name": "Economy",
            "carrier_code": "stamps_com", "service_code": "usps_priority_mail",
            "package_code": "large_flat_rate_box", "confirmation": "none", "pdx_carrier": "USPS",
        }), content_type="application/json")
        assert resp.get_json()["ok"] is True
        mappings = client.get(f"/api/get_shipping_option_mappings?provider_id={pid}").get_json()
        assert mappings[0]["pdx_carrier"] == "USPS"

    def test_save_shipping_option_mapping_requires_fields(self, client):
        resp = client.post("/api/save_shipping_option_mapping", data=json.dumps({}), content_type="application/json")
        assert resp.get_json()["ok"] is False

    def test_get_shipping_option_mappings_no_provider_id(self, client):
        assert client.get("/api/get_shipping_option_mappings").get_json() == []


class TestMarkReadyToShip:
    def _order(self, num="ORD001", option_external_id="pdx_economy"):
        return {
            "num": num, "gallery": "G", "status": "received",
            "placedAt": "2026-01-01T00:00:00Z", "items": [],
            "shipping": {"option": {"externalId": option_external_id, "name": "Economy"},
                        "destination": {"recipient": "Jane Doe", "address1": "123 Main St",
                                       "city": "Orlando", "state": "FL", "zipCode": "32789"}},
        }

    def _provider_with_mapping(self, option_external_id="pdx_economy", pdx_carrier="USPS"):
        pid = db.upsert_shipping_provider("shipstation", "SS", {"api_key": "k", "api_secret": "s"})
        db.upsert_shipping_option_mapping(
            pid, option_external_id, "Economy", "stamps_com", "usps_priority_mail", "", "none", pdx_carrier,
        )
        return pid

    def test_order_not_found(self, client):
        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "NOPE"}), content_type="application/json")
        assert resp.get_json()["ok"] is False

    def test_already_shipped_is_rejected(self, client):
        db.upsert_order(self._order())
        db.record_shipped_notification("ORD001", "USPS", "9400", "manual")
        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        assert resp.get_json()["ok"] is False

    def test_no_provider_configured(self, client):
        db.upsert_order(self._order())
        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        assert resp.get_json()["ok"] is False

    def test_no_mapping_for_shipping_option(self, client):
        db.upsert_order(self._order(option_external_id="pdx_expedited"))
        self._provider_with_mapping(option_external_id="pdx_economy")  # different option
        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert "mapping" in data["error"].lower()

    def test_rejects_mapping_with_no_pdx_carrier_before_buying_a_label(self, client, monkeypatch):
        """Regression test for a real bug found by code review: a mapping can
        have a real carrier_code/service_code set while pdx_carrier is left
        "— Do not map —" (saved as null) in Settings. Without this check,
        Ready to Ship would buy a real label, then fail the PDX callback
        with carrier=None — a paid, orphaned label with nothing to show for
        it. This must be caught before create_label is ever called."""
        import shipping_providers as sp
        db.upsert_order(self._order())
        pid = db.upsert_shipping_provider("shipstation", "SS", {"api_key": "k", "api_secret": "s"})
        db.upsert_shipping_option_mapping(pid, "pdx_economy", "Economy", "stamps_com", "usps_priority_mail", "", "none", None)
        create_label_calls = []
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label",
                           lambda self, *a, **k: (create_label_calls.append(1), ({"tracking_number": "X"}, ""))[1])

        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert "PDX Carrier" in data["error"]
        assert not create_label_calls

    def test_concurrent_requests_for_the_same_order_only_buy_one_label(self, client, monkeypatch):
        """Regression test for a real race found by code review: two
        near-simultaneous requests for the same order (now a real
        possibility with multi-station sharing one backend, or just a
        double-click) used to both pass has_shipped_notification() before
        either recorded it, each buying a real label. A lock around the
        whole check-through-record section closes that window."""
        import shipping_providers as sp
        import api as pdx_api
        import time

        db.upsert_order(self._order())
        self._provider_with_mapping()
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order", lambda self, order: ("555", ""))
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))
        create_label_calls = []

        def slow_create_label(self, *a, **k):
            create_label_calls.append(1)
            time.sleep(0.15)  # widen the race window so both threads overlap
            return {"tracking_number": "9400123"}, ""
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label", slow_create_label)

        results = []
        def call():
            resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
            results.append(resp.get_json())

        t1 = threading.Thread(target=call)
        t2 = threading.Thread(target=call)
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert len(create_label_calls) == 1, "a real label must only ever be purchased once for the same order"
        oks = [r["ok"] for r in results]
        assert oks.count(True) == 1
        assert oks.count(False) == 1

    def test_success_creates_order_and_label_and_confirms(self, client, monkeypatch):
        import shipping_providers as sp
        import api as pdx_api
        db.upsert_order(self._order())
        self._provider_with_mapping()
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order", lambda self, order: ("555", ""))
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label",
                           lambda self, *a, **k: ({"tracking_number": "9400123", "shipment_cost": 8.5, "label_data": "ZmFrZXBkZg=="}, ""))
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))

        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["tracking_number"] == "9400123"
        assert data["carrier"] == "USPS"
        assert db.get_order("ORD001")["ship_label_data"] == "ZmFrZXBkZg=="
        assert db.has_shipped_notification("ORD001") is True
        assert db.get_order("ORD001")["status"] == "fulfilled"

    def test_uses_default_weight_when_none_provided(self, client, monkeypatch):
        import shipping_providers as sp
        import api as pdx_api
        import config
        db.upsert_order(self._order())
        self._provider_with_mapping()
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order", lambda self, order: ("555", ""))
        captured = {}
        def fake_create_label(self, external_order_id, carrier_code, service_code, package_code,
                              confirmation, ship_date, weight_lb, test_label=False):
            captured["weight_lb"] = weight_lb
            return {"tracking_number": "9400123"}, ""
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label", fake_create_label)
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))

        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert captured["weight_lb"] == config.load()["default_package_weight_lb"]

    def test_staff_supplied_weight_overrides_default(self, client, monkeypatch):
        import shipping_providers as sp
        import api as pdx_api
        db.upsert_order(self._order())
        self._provider_with_mapping()
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order", lambda self, order: ("555", ""))
        captured = {}
        def fake_create_label(self, external_order_id, carrier_code, service_code, package_code,
                              confirmation, ship_date, weight_lb, test_label=False):
            captured["weight_lb"] = weight_lb
            return {"tracking_number": "9400123"}, ""
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label", fake_create_label)
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))

        resp = client.post("/api/mark_ready_to_ship",
                           data=json.dumps({"order_num": "ORD001", "weight_lb": 2.5}), content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert captured["weight_lb"] == 2.5

    def test_rejects_zero_or_negative_weight_before_buying_a_label(self, client, monkeypatch):
        import shipping_providers as sp
        db.upsert_order(self._order())
        self._provider_with_mapping()
        create_label_calls = []
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label",
                           lambda self, *a, **k: (create_label_calls.append(1), ({"tracking_number": "X"}, ""))[1])

        resp = client.post("/api/mark_ready_to_ship",
                           data=json.dumps({"order_num": "ORD001", "weight_lb": 0}), content_type="application/json")
        assert resp.get_json()["ok"] is False
        assert not create_label_calls

    def test_reuses_existing_provider_order_id_without_recreating(self, client, monkeypatch):
        import shipping_providers as sp
        import api as pdx_api
        db.upsert_order(self._order())
        pid = self._provider_with_mapping()
        db.set_order_ship_provider("ORD001", pid, "999")
        create_order_calls = []
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order",
                           lambda self, order: (create_order_calls.append(1), ("should not be used", ""))[1])
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label",
                           lambda self, *a, **k: ({"tracking_number": "9400123"}, ""))
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (True, ""))

        client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        assert create_order_calls == []  # existing external_order_id was reused

    def test_label_creation_failure_does_not_confirm(self, client, monkeypatch):
        import shipping_providers as sp
        db.upsert_order(self._order())
        self._provider_with_mapping()
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order", lambda self, order: ("555", ""))
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label", lambda self, *a, **k: (None, "carrier down"))

        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        assert resp.get_json()["ok"] is False
        assert db.has_shipped_notification("ORD001") is False
        assert db.get_order("ORD001")["status"] == "received"

    def test_pdx_rejection_does_not_confirm_but_label_already_bought(self, client, monkeypatch):
        import shipping_providers as sp
        import api as pdx_api
        db.upsert_order(self._order())
        self._provider_with_mapping()
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order", lambda self, order: ("555", ""))
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label",
                           lambda self, *a, **k: ({"tracking_number": "9400123"}, ""))
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (False, "HTTP 500"))

        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert "9400123" in data["error"]  # surfaced so staff can Mark Shipped manually with this tracking number
        assert db.has_shipped_notification("ORD001") is False

    def test_pdx_already_shipped_rejection_is_treated_as_success(self, client, monkeypatch):
        import shipping_providers as sp
        import api as pdx_api
        db.upsert_order(self._order())
        self._provider_with_mapping()
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order", lambda self, order: ("555", ""))
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label",
                           lambda self, *a, **k: ({"tracking_number": "9400123"}, ""))
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (False, "Order already shipped"))

        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        assert resp.get_json()["ok"] is True
        assert db.has_shipped_notification("ORD001") is True

    def test_hand_delivered_mapping_never_touches_shipstation(self, client, monkeypatch):
        """A bulk order dropped off in person at a school should never buy a
        real label — this mapping skips ShipStation entirely and reports
        straight to PDX."""
        import shipping_providers as sp
        import api as pdx_api
        db.upsert_order(self._order(option_external_id="economy-bulk"))
        self._provider_with_mapping(option_external_id="economy-bulk", pdx_carrier="HAND_DELIVERED")
        create_order_calls = []
        create_label_calls = []
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order",
                           lambda self, order: (create_order_calls.append(1), ("should not be used", ""))[1])
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label",
                           lambda self, *a, **k: (create_label_calls.append(1), ({"tracking_number": "X"}, ""))[1])
        pdx_calls = []
        monkeypatch.setattr(pdx_api, "shipped_callback",
                           lambda lab_id, api_key, order_num, carrier="Pickup", tracking_number="":
                               (pdx_calls.append((order_num, carrier, tracking_number)), (True, ""))[1])

        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["carrier"] == "Hand Delivered"
        assert data["tracking_number"] == ""
        assert not create_order_calls
        assert not create_label_calls
        assert pdx_calls == [("ORD001", "Hand Delivered", "")]
        assert db.has_shipped_notification("ORD001") is True
        assert db.get_order("ORD001")["status"] == "fulfilled"

    def test_hand_delivered_pdx_failure_does_not_confirm(self, client, monkeypatch):
        import api as pdx_api
        db.upsert_order(self._order(option_external_id="economy-bulk"))
        self._provider_with_mapping(option_external_id="economy-bulk", pdx_carrier="HAND_DELIVERED")
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (False, "bad api key"))

        resp = client.post("/api/mark_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert db.has_shipped_notification("ORD001") is False
        assert db.get_order("ORD001")["status"] != "fulfilled"


class TestGetShippingLabel:
    def test_404_when_no_label_on_file(self, client):
        db.upsert_order({"num": "ORD001", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "economy"}, "destination": {"recipient": "C"}}})
        resp = client.get("/api/get_shipping_label?order_num=ORD001")
        assert resp.status_code == 404

    def test_404_for_unknown_order(self, client):
        resp = client.get("/api/get_shipping_label?order_num=NOPE")
        assert resp.status_code == 404

    def test_returns_stored_label_as_pdf(self, client):
        import base64
        db.upsert_order({"num": "ORD001", "gallery": "G", "status": "received",
                         "placedAt": "2026-01-01T00:00:00Z", "items": [],
                         "shipping": {"option": {"externalId": "economy"}, "destination": {"recipient": "C"}}})
        db.save_order_ship_label("ORD001", base64.b64encode(b"%PDF-fake").decode())
        resp = client.get("/api/get_shipping_label?order_num=ORD001")
        assert resp.status_code == 200
        assert resp.mimetype == "application/pdf"
        assert resp.data == b"%PDF-fake"


class TestReadyToShipTestMode:
    """Pure dry-run: proves a shipping-provider mapping works without ever
    touching the real order's provider linkage, PDX, or local status."""

    def _order(self, num="ORD001", option_external_id="pdx_economy"):
        return {
            "num": num, "gallery": "G", "status": "received",
            "placedAt": "2026-01-01T00:00:00Z", "items": [],
            "shipping": {"option": {"externalId": option_external_id, "name": "Economy"},
                        "destination": {"recipient": "Jane Doe", "address1": "123 Main St",
                                       "city": "Orlando", "state": "FL", "zipCode": "32789"}},
        }

    def _provider_with_mapping(self, option_external_id="pdx_economy", pdx_carrier="USPS"):
        pid = db.upsert_shipping_provider("shipstation", "SS", {"api_key": "k", "api_secret": "s"})
        db.upsert_shipping_option_mapping(
            pid, option_external_id, "Economy", "stamps_com", "usps_priority_mail", "", "none", pdx_carrier,
        )
        return pid

    def test_order_not_found(self, client):
        resp = client.post("/api/test_ready_to_ship", data=json.dumps({"order_num": "NOPE"}), content_type="application/json")
        assert resp.get_json()["ok"] is False

    def test_no_provider_configured(self, client):
        db.upsert_order(self._order())
        resp = client.post("/api/test_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        assert resp.get_json()["ok"] is False

    def test_no_mapping_for_shipping_option(self, client):
        db.upsert_order(self._order(option_external_id="pdx_expedited"))
        self._provider_with_mapping(option_external_id="pdx_economy")
        resp = client.post("/api/test_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert "mapping" in data["error"].lower()

    def test_hand_delivered_mapping_has_nothing_to_test(self, client, monkeypatch):
        import shipping_providers as sp
        db.upsert_order(self._order(option_external_id="economy-bulk"))
        self._provider_with_mapping(option_external_id="economy-bulk", pdx_carrier="HAND_DELIVERED")
        create_order_calls = []
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order",
                           lambda self, order: (create_order_calls.append(1), ("x", ""))[1])

        resp = client.post("/api/test_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert "hand delivered" in data["error"].lower()
        assert not create_order_calls

    def test_success_does_not_touch_real_order_state(self, client, monkeypatch):
        import shipping_providers as sp
        import api as pdx_api
        db.upsert_order(self._order())
        self._provider_with_mapping()

        create_order_calls = []
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order",
                           lambda self, order: (create_order_calls.append(order), ("TEST-999", ""))[1])
        create_label_calls = []
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label",
                           lambda self, *a, **k: (create_label_calls.append((a, k)),
                                                  ({"tracking_number": "TESTTRACK", "label_data": "ZmFrZQ=="}, ""))[1])
        pdx_calls = []
        monkeypatch.setattr(pdx_api, "shipped_callback", lambda *a, **k: (pdx_calls.append(1), (True, ""))[1])

        resp = client.post("/api/test_ready_to_ship",
                           data=json.dumps({"order_num": "ORD001", "weight_lb": 1.0}), content_type="application/json")
        data = resp.get_json()

        assert data["ok"] is True
        assert data["tracking_number"] == "TESTTRACK"
        assert data["label_data"] == "ZmFrZQ=="
        # Never reports to PDX and never mutates the real order's local state —
        # the whole point of a dry run.
        assert not pdx_calls
        assert db.has_shipped_notification("ORD001") is False
        assert db.get_order("ORD001")["ship_external_order_id"] is None
        assert db.get_order("ORD001")["ship_label_data"] is None
        assert db.get_order("ORD001")["status"] != "fulfilled"
        # Uses a disposable order number, never the real one, and passes
        # test_label=True through to create_label.
        assert create_order_calls[0]["order_num"] == "ORD001-TEST"
        assert create_label_calls[0][1]["test_label"] is True

    def test_label_creation_failure_is_reported(self, client, monkeypatch):
        import shipping_providers as sp
        db.upsert_order(self._order())
        self._provider_with_mapping()
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_order", lambda self, order: ("TEST-999", ""))
        monkeypatch.setattr(sp.ShipStationV1Adapter, "create_label", lambda self, *a, **k: (None, "carrier down"))

        resp = client.post("/api/test_ready_to_ship", data=json.dumps({"order_num": "ORD001"}), content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False
        assert data["error"] == "carrier down"


# ── Multi-station (same-location, onsite-only) ────────────────────────────────
# discovery.discover_stations and requests.post (the inter-station handoff
# call) are mocked throughout — real UDP broadcast isn't reliably available
# in a sandboxed/CI environment (see tests/test_discovery.py, which covers
# the real wire protocol over loopback instead). DiscoveryResponder.start/stop
# are also mocked so tests never bind a real socket.

class TestMultiStation:
    @pytest.fixture(autouse=True)
    def _no_real_network(self, monkeypatch):
        import discovery
        monkeypatch.setattr(discovery.DiscoveryResponder, "start", lambda self: None)
        monkeypatch.setattr(discovery.DiscoveryResponder, "stop", lambda self: None)
        monkeypatch.setattr(discovery, "discover_stations", lambda **kw: [])

    def test_station_info_defaults_to_solo(self, client):
        data = client.get("/api/station/info").get_json()
        assert data["role"] == "solo"
        assert data["name"] == ""
        assert data["joined_primary_url"] == ""

    def test_become_primary_requires_name(self, client):
        resp = client.post("/api/station/become_primary", json={})
        assert resp.get_json()["ok"] is False

    def test_become_primary_fails_cleanly_when_lan_address_undeterminable(self, client, monkeypatch):
        """Regression test for a real bug found by code review: get_lan_ip()
        used to fall back to "127.0.0.1" on failure (e.g. no default
        gateway — an isolated event LAN) instead of None. That address would
        get broadcast as this station's own, and every other station trying
        to join it would silently connect to their own loopback instead of
        the real primary — undiscoverable and unexplained. Becoming primary
        with no real address must fail loudly instead."""
        import server as server_module
        monkeypatch.setattr(server_module, "get_lan_ip", lambda: None)

        resp = client.post("/api/station/become_primary", json={"name": "Front Desk"})
        data = resp.get_json()
        assert data["ok"] is False
        assert "network address" in data["error"].lower()

        info = client.get("/api/station/info").get_json()
        assert info["role"] == "solo"  # never promoted

    def test_become_primary_sets_role(self, client):
        resp = client.post("/api/station/become_primary", json={"name": "Front Desk"})
        data = resp.get_json()
        assert data["ok"] is True
        assert data["other_primary_found"] is None

        info = client.get("/api/station/info").get_json()
        assert info["role"] == "primary"
        assert info["name"] == "Front Desk"

    def test_become_primary_starts_poller_when_credentials_exist(self, client, app):
        client.post("/api/save_settings", json={"lab_id": "LAB1", "api_key": "KEY1"})
        client.post("/api/station/become_primary", json={"name": "Front Desk"})
        # The MockPoller instance server.py is holding — reach it the same
        # way other tests confirm poller state.
        assert client.get("/api/get_poller_status").get_json()["running"] is True

    def test_become_primary_preserves_other_settings(self, client):
        """config.save() merges onto the currently-saved config, not onto
        bare DEFAULTS — otherwise this partial station-role update would
        silently wipe lab_id/studio_name/etc. back to defaults."""
        client.post("/api/save_settings", json={"lab_id": "LAB1", "api_key": "KEY1", "studio_name": "My Studio"})
        client.post("/api/station/become_primary", json={"name": "Front Desk"})
        cfg = client.get("/api/get_settings").get_json()
        assert cfg["lab_id"] == "LAB1"
        assert cfg["studio_name"] == "My Studio"

    def test_normal_settings_save_does_not_reset_station_role(self, client):
        """Regression test for a real bug found by code review: the normal
        Settings-page Save button only ever submits the settings-form fields
        — it has no idea station_role/station_name exist — so saving
        settings after becoming primary must not silently demote this
        station back to solo."""
        client.post("/api/station/become_primary", json={"name": "Front Desk"})
        client.post("/api/save_settings", json={"lab_id": "LAB1", "api_key": "KEY1", "studio_name": "My Studio"})

        info = client.get("/api/station/info").get_json()
        assert info["role"] == "primary"
        assert info["name"] == "Front Desk"

    def test_become_primary_no_warning_when_nothing_else_found(self, client):
        client.post("/api/station/become_primary", json={"name": "Front Desk"})
        log = client.get("/api/activity_log").get_json()
        assert not any("Another primary" in e["message"] for e in log)

    def test_become_primary_handoff_succeeds_when_another_primary_reachable(self, client, monkeypatch):
        import discovery
        import requests

        monkeypatch.setattr(discovery, "discover_stations",
                            lambda **kw: [{"name": "Old Primary", "url": "http://10.0.0.5:5050", "studio_name": ""}])
        calls = []

        class FakeResp:
            def json(self_inner):
                return {"ok": True}

        def fake_post(url, json=None, timeout=None):
            calls.append((url, json))
            return FakeResp()

        monkeypatch.setattr(requests, "post", fake_post)

        resp = client.post("/api/station/become_primary", json={"name": "Check-in 2"})
        data = resp.get_json()
        assert data["ok"] is True
        assert data["other_primary_found"] is None  # handoff succeeded, so no conflict remains

        assert len(calls) == 1
        url, payload = calls[0]
        assert url == "http://10.0.0.5:5050/api/station/step_down"
        assert payload["new_primary_name"] == "Check-in 2"

        info = client.get("/api/station/info").get_json()
        assert info["role"] == "primary"

    def test_become_primary_falls_back_to_soft_warning_when_handoff_unreachable(self, client, monkeypatch):
        import discovery
        import requests

        monkeypatch.setattr(discovery, "discover_stations",
                            lambda **kw: [{"name": "Old Primary", "url": "http://10.0.0.5:5050", "studio_name": ""}])

        def fake_post(*a, **k):
            raise ConnectionError("unreachable")
        monkeypatch.setattr(requests, "post", fake_post)

        resp = client.post("/api/station/become_primary", json={"name": "Check-in 2"})
        data = resp.get_json()
        assert data["ok"] is True  # emergency promotion still proceeds
        assert data["other_primary_found"]["name"] == "Old Primary"

        info = client.get("/api/station/info").get_json()
        assert info["role"] == "primary"  # promotion happened despite the conflict

        log = client.get("/api/activity_log").get_json()
        assert any("Old Primary" in e["message"] and e["level"] == "error" for e in log)

    def test_become_primary_returns_error_when_handoff_explicitly_refused(self, client, monkeypatch):
        import discovery
        import requests

        monkeypatch.setattr(discovery, "discover_stations",
                            lambda **kw: [{"name": "Old Primary", "url": "http://10.0.0.5:5050", "studio_name": ""}])

        class FakeResp:
            def json(self_inner):
                return {"ok": False, "error": "This station is not currently the primary"}

        monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp())

        resp = client.post("/api/station/become_primary", json={"name": "Check-in 2"})
        data = resp.get_json()
        assert data["ok"] is False

        info = client.get("/api/station/info").get_json()
        assert info["role"] == "solo"  # never promoted — the refusal must block it

    def test_station_join_requires_name_and_primary_url(self, client):
        resp = client.post("/api/station/join", json={"name": "Check-in 2"})
        assert resp.get_json()["ok"] is False

    def test_station_join_sets_secondary_role(self, client):
        resp = client.post("/api/station/join",
                           json={"name": "Check-in 2", "primary_url": "http://10.0.0.5:5050", "primary_name": "Front Desk"})
        assert resp.get_json()["ok"] is True

        info = client.get("/api/station/info").get_json()
        assert info["role"] == "secondary"
        assert info["name"] == "Check-in 2"
        assert info["joined_primary_url"] == "http://10.0.0.5:5050"

    def test_step_down_requires_currently_being_primary(self, client):
        resp = client.post("/api/station/step_down",
                           json={"new_primary_url": "http://x:5050", "new_primary_name": "X"})
        assert resp.get_json()["ok"] is False

    def test_step_down_demotes_and_pushes_redirect_event(self, client):
        client.post("/api/station/become_primary", json={"name": "Front Desk"})

        with client.get("/api/events") as resp:
            it = resp.iter_encoded()
            next(it)  # "connected" handshake

            step_down = client.post("/api/station/step_down",
                                    json={"new_primary_url": "http://10.0.0.9:5050", "new_primary_name": "New Primary"})
            assert step_down.get_json()["ok"] is True

            # step_down logs an activity line (pushed first) THEN the
            # station_demoted redirect event — read both, don't assume order.
            combined = next(it) + next(it)
            assert b"station_demoted" in combined
            assert b"10.0.0.9" in combined

        info = client.get("/api/station/info").get_json()
        assert info["role"] == "secondary"
        assert info["joined_primary_url"] == "http://10.0.0.9:5050"
        assert info["name"] == "Front Desk"  # this station's own name survives the demotion

    def test_reset_to_solo_clears_role_and_stops_poller(self, client):
        client.post("/api/save_settings", json={"lab_id": "LAB1", "api_key": "KEY1"})
        client.post("/api/station/become_primary", json={"name": "Front Desk"})
        assert client.get("/api/get_poller_status").get_json()["running"] is True

        resp = client.post("/api/station/reset_to_solo")
        assert resp.get_json()["ok"] is True

        info = client.get("/api/station/info").get_json()
        assert info["role"] == "solo"
        assert info["name"] == ""
        assert info["joined_primary_url"] == ""
        assert client.get("/api/get_poller_status").get_json()["running"] is False

    def test_discover_endpoint_excludes_self(self, client, monkeypatch):
        import discovery
        import server as server_module
        my_url = f"http://{server_module.get_lan_ip()}:5050"
        monkeypatch.setattr(discovery, "discover_stations",
                            lambda **kw: [{"name": "Me", "url": my_url, "studio_name": ""},
                                         {"name": "Someone Else", "url": "http://10.0.0.5:5050", "studio_name": ""}])
        data = client.get("/api/station/discover").get_json()
        names = [s["name"] for s in data["stations"]]
        assert "Me" not in names
        assert "Someone Else" in names

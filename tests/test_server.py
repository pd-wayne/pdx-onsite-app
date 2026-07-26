"""
test_server.py — Flask route integration tests.

Uses Flask's test client with an isolated DB and mock poller.
Every test gets a fresh DB via the `client` fixture in conftest.py.
"""
import io
import json
import pytest
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
        assert data["fulfillment_mode"] == "pickup"

    def test_get_settings_contains_all_expected_keys(self, client):
        data = client.get("/api/get_settings").get_json()
        for key in ("lab_id", "api_key", "studio_name", "poll_interval",
                    "fulfillment_mode", "printer_name", "image_output_folder"):
            assert key in data, f"Missing key: {key}"

    def test_save_settings_returns_ok(self, client):
        resp = client.post("/api/save_settings",
                           data=json.dumps({"lab_id": "x", "poll_interval": 60}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_save_then_get_roundtrip(self, client):
        payload = {
            "lab_id": "lab123",
            "fulfillment_mode": "dropship",
            "poll_interval": 30,
            "studio_name": "Wayne's Photos",
        }
        client.post("/api/save_settings",
                    data=json.dumps(payload), content_type="application/json")
        data = client.get("/api/get_settings").get_json()
        assert data["lab_id"] == "lab123"
        assert data["fulfillment_mode"] == "dropship"
        assert data["poll_interval"] == 30

    def test_save_fulfillment_mode_both(self, client):
        client.post("/api/save_settings",
                    data=json.dumps({"fulfillment_mode": "both"}),
                    content_type="application/json")
        assert client.get("/api/get_settings").get_json()["fulfillment_mode"] == "both"

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
        data = client.get("/api/get_version").get_json()
        assert data["version"] == "2.0.10"

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

    def test_sse_endpoint_content_type(self, client):
        with client.get("/api/events") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.content_type

    def test_samples_list_no_folder(self, client):
        data = client.get("/api/samples/list").get_json()
        assert "files" in data
        assert data["files"] == []

    def test_image_not_found(self, client):
        resp = client.get("/api/image/ORD001/fake.jpg")
        assert resp.status_code in (404, 404)

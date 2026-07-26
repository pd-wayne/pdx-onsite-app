"""
test_poller.py — Tests for order fulfillment mode classification.

The old global mode filter (_is_pickup_order, _order_matches_mode) was removed in
Phase 3.  Classification now happens inline in db.upsert_order() and is stored as
orders.fulfillment_mode.  These tests exercise that classification via the DB layer.
"""
import json
import os
import tempfile
import threading
import pytest

import api as pdx_api
import config
import db
import poller as poller_module
import printer

# ── Minimal order shapes ──────────────────────────────────────────────────────

PICKUP = {"num": "T-001", "gallery": "g1",
          "shipping": {"option": {"externalId": "pdx_pickup"},
                       "destination": {"recipient": "Alice"}}}

ECONOMY = {"num": "T-002", "gallery": "g1",
           "shipping": {"option": {"externalId": "economy"},
                        "destination": {"recipient": "Bob"}}}

OVERNIGHT = {"num": "T-003", "gallery": "g1",
             "shipping": {"option": {"externalId": "overnight"},
                          "destination": {"recipient": "Carol"}}}

NO_SHIPPING = {"num": "T-004", "gallery": "g1"}

MISSING_OPTION = {"num": "T-005", "gallery": "g1", "shipping": {}}

MISSING_EXTERNAL_ID = {"num": "T-006", "gallery": "g1",
                       "shipping": {"option": {}}}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Each test gets its own fresh SQLite database."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    yield


def _mode(order_data: dict) -> str:
    """Upsert an order and return its stored fulfillment_mode."""
    db.upsert_order(order_data)
    order_num = order_data.get("num") or order_data.get("order_num")
    row = db.get_order(order_num)
    return row["fulfillment_mode"] if row else None


# ── fulfillment_mode classification ──────────────────────────────────────────

class TestFulfillmentModeClassification:
    def test_pickup_externalid_stored_as_pickup(self):
        assert _mode(PICKUP) == "pickup"

    def test_economy_shipping_stored_as_dropship(self):
        assert _mode(ECONOMY) == "dropship"

    def test_overnight_shipping_stored_as_dropship(self):
        assert _mode(OVERNIGHT) == "dropship"

    def test_missing_shipping_stored_as_dropship(self):
        assert _mode(NO_SHIPPING) == "dropship"

    def test_missing_option_stored_as_dropship(self):
        assert _mode(MISSING_OPTION) == "dropship"

    def test_missing_external_id_stored_as_dropship(self):
        assert _mode(MISSING_EXTERNAL_ID) == "dropship"

    def test_real_pickup_order(self, pickup_order):
        assert _mode(pickup_order) == "pickup"

    def test_real_dropship_order(self, dropship_order):
        assert _mode(dropship_order) == "dropship"


# ── is_bulk classification ────────────────────────────────────────────────────
# A studio owner uses this to spot, at a glance in the queue, which orders are
# bulk shipments (multiple grouped sub-orders bound for one organization) vs a
# single order — independent of pickup/dropship. Derived from a non-empty
# groups[] on the raw PDX payload (isBulkOrder isn't reliable — confirmed
# absent/inconsistent against real samples).

def _is_bulk(order_data: dict) -> bool:
    db.upsert_order(order_data)
    order_num = order_data.get("num") or order_data.get("order_num")
    row = db.get_order(order_num)
    return bool(row["is_bulk"]) if row else None


BULK_WITH_GROUPS = {"num": "T-010", "gallery": "g2",
                    "shipping": {"option": {"externalId": "pdx_bulk_nextday"},
                                 "destination": {"recipient": "Production Facility"}},
                    "groups": [{"id": 1, "fields": [{"key": "first_name", "value": "A"}]}]}

NO_GROUPS_KEY = {"num": "T-011", "gallery": "g2",
                 "shipping": {"option": {"externalId": "pdx_ECON"},
                              "destination": {"recipient": "Someone"}}}

EMPTY_GROUPS = {"num": "T-012", "gallery": "g2",
                "shipping": {"option": {"externalId": "pdx_pickup"},
                             "destination": {"recipient": "Someone"}},
                "groups": []}


class TestIsBulkClassification:
    def test_non_empty_groups_is_bulk(self):
        assert _is_bulk(BULK_WITH_GROUPS) is True

    def test_missing_groups_key_is_not_bulk(self):
        assert _is_bulk(NO_GROUPS_KEY) is False

    def test_empty_groups_is_not_bulk(self):
        assert _is_bulk(EMPTY_GROUPS) is False

    def test_bulk_is_independent_of_pickup_dropship(self):
        # A pickup order can still be bulk (groups present) — is_bulk and
        # fulfillment_mode are two separate axes, not derived from each other.
        pickup_bulk = {"num": "T-013", "gallery": "g2",
                       "shipping": {"option": {"externalId": "pdx_pickup"},
                                    "destination": {"recipient": "Someone"}},
                       "groups": [{"id": 1, "fields": []}]}
        assert _mode(pickup_bulk) == "pickup"
        assert _is_bulk(pickup_bulk) is True

    def test_real_bulk_sample_is_bulk(self):
        real_bulk = {
            "num": "BAP1752155240", "gallery": "PDX Bulk Drop",
            "shipping": {"option": {"externalId": "pdx_bulk_nextday"},
                        "destination": {"recipient": "Production Facility"}},
            "groups": [
                {"id": 1, "fields": [{"key": "first_name", "value": "Jamie"}]},
                {"id": 2, "fields": [{"key": "first_name", "value": "Jamie"}]},
            ],
        }
        assert _is_bulk(real_bulk) is True

    def test_real_non_bulk_sample_with_groupid_is_not_bulk(self):
        # order ZI1784299034: every item carries a groupId, groups itself is []
        real_non_bulk = {
            "num": "ZI1784299034", "gallery": "PDX Bulk Test",
            "shipping": {"option": {"externalId": "pdx_ECON"},
                        "destination": {"recipient": "Martha R. Piovesan"}},
            "groups": [],
        }
        assert _is_bulk(real_non_bulk) is False


# ── Job mode derived from first order ────────────────────────────────────────

class TestJobModeDerivation:
    def test_pickup_order_creates_onsite_job(self):
        db.upsert_order(PICKUP)
        job = db.get_job("g1")
        assert job["fulfillment_mode"] == "onsite"

    def test_dropship_order_creates_in_studio_job(self):
        db.upsert_order(ECONOMY)
        job = db.get_job("g1")
        assert job["fulfillment_mode"] == "in_studio"

    def test_subsequent_order_does_not_override_job_mode(self):
        # First pickup order sets job to onsite
        db.upsert_order(PICKUP)
        # Second dropship order must not flip the job to in_studio
        dropship = dict(ECONOMY)
        dropship["num"] = "T-999"
        db.upsert_order(dropship)
        job = db.get_job("g1")
        assert job["fulfillment_mode"] == "onsite"


# ── Dropship image download (onsite job) ──────────────────────────────────────
# Dropship orders on an onsite job get no receipt and must never land in a
# routed hot folder (that would trigger DNP auto-print) — images are fetched
# into a separate dropship/ORDER_NUM/ folder for the studio to print manually.

class TestDropshipImageDownload:
    def test_downloads_into_dropship_subfolder_not_hot_folder(self, monkeypatch, tmp_path):
        captured = {}

        def fake_download_images(images, folder, order_num="", api_key=""):
            captured["folder"] = folder
            return True, ""

        monkeypatch.setattr(printer, "download_images", fake_download_images)
        monkeypatch.setattr(config, "load", lambda: {"image_output_folder": str(tmp_path / "Hot")})

        p = poller_module.Poller()
        images = [{"filename": "a.jpg", "assetUrl": "https://example.com/a.jpg"}]
        p._download_dropship_images("DS001", images, api_key="fake")

        assert captured["folder"] == os.path.join(str(tmp_path / "Hot"), "dropship", "DS001")
        assert not os.path.exists(str(tmp_path / "Hot" / "a.jpg"))

    def test_no_images_marks_ok_without_downloading(self, monkeypatch):
        monkeypatch.setattr(
            printer, "download_images",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
        )
        db.upsert_order(PICKUP)
        p = poller_module.Poller()
        p._download_dropship_images("T-001", [], api_key="fake")
        assert db.get_order("T-001")["download_status"] == "ok"

    def test_no_output_folder_marks_failed(self, monkeypatch):
        monkeypatch.setattr(config, "load", lambda: {"image_output_folder": ""})
        db.upsert_order(PICKUP)
        p = poller_module.Poller()
        images = [{"filename": "a.jpg", "assetUrl": "https://example.com/a.jpg"}]
        p._download_dropship_images("T-001", images, api_key="fake")
        assert db.get_order("T-001")["download_status"] == "failed"

    def test_calls_on_download_done_callback(self, monkeypatch, tmp_path):
        monkeypatch.setattr(printer, "download_images", lambda *a, **k: (True, ""))
        monkeypatch.setattr(config, "load", lambda: {"image_output_folder": str(tmp_path / "Hot")})

        results = []
        p = poller_module.Poller(on_download_done=lambda num, ok, err: results.append((num, ok, err)))
        images = [{"filename": "a.jpg", "assetUrl": "https://example.com/a.jpg"}]
        p._download_dropship_images("DS002", images, api_key="fake")

        assert results == [("DS002", True, "")]


# ── In-studio routing (Phase 6) ───────────────────────────────────────────────
# In-studio pickup orders now get the exact same hot-folder/product-routing
# treatment onsite orders do — the only thing that stays onsite-only is the
# receipt. A packing slip is printed on-demand instead (see server.py).

class _SyncThread:
    """Runs the target synchronously instead of spawning a real thread, so
    _do_poll's fire-and-forget download threads are deterministic in tests."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


class TestInStudioRouting:
    def _poll_one_order(self, monkeypatch, order_data, job_mode):
        monkeypatch.setattr(threading, "Thread", _SyncThread)
        monkeypatch.setattr(config, "load", lambda: {"print_mode": "auto"})
        monkeypatch.setattr(pdx_api, "poll_orders", lambda lab_id, api_key: ([order_data], None))

        receipt_calls = []
        monkeypatch.setattr(poller_module.Poller, "_print_receipt",
                            lambda self, *a, **k: receipt_calls.append(a))

        download_calls = []
        monkeypatch.setattr(printer, "download_images",
                            lambda images, folder, order_num="", api_key="":
                                (download_calls.append(order_num) or (True, "")))

        gallery = order_data["gallery"]
        db.upsert_job(gallery, default_mode=job_mode)
        db.seed_default_destination("/tmp/hotfolder")

        p = poller_module.Poller()
        p.lab_id = "lab"
        p.api_key = "key"
        p._do_poll()

        return receipt_calls, download_calls

    def test_in_studio_pickup_order_gets_routed_download_no_receipt(self, monkeypatch, pickup_order):
        receipt_calls, download_calls = self._poll_one_order(monkeypatch, pickup_order, "in_studio")

        assert receipt_calls == []
        assert pickup_order["num"] in download_calls
        items = db.get_order_items(pickup_order["num"])
        assert len(items) >= 1

    def test_onsite_pickup_order_still_gets_receipt(self, monkeypatch, pickup_order):
        receipt_calls, download_calls = self._poll_one_order(monkeypatch, pickup_order, "onsite")

        assert len(receipt_calls) == 1
        assert pickup_order["num"] in download_calls

    def test_in_studio_dropship_order_gets_no_receipt_no_slip(self, monkeypatch, dropship_order):
        # Dropship handling is identical regardless of job_mode — this just confirms
        # an in_studio job doesn't change that path.
        monkeypatch.setattr(threading, "Thread", _SyncThread)
        monkeypatch.setattr(config, "load", lambda: {"print_mode": "auto", "image_output_folder": "/tmp/hot"})
        monkeypatch.setattr(pdx_api, "poll_orders", lambda lab_id, api_key: ([dropship_order], None))

        receipt_calls = []
        monkeypatch.setattr(poller_module.Poller, "_print_receipt",
                            lambda self, *a, **k: receipt_calls.append(a))
        monkeypatch.setattr(printer, "download_images", lambda *a, **k: (True, ""))

        gallery = dropship_order["gallery"]
        db.upsert_job(gallery, default_mode="in_studio")

        p = poller_module.Poller()
        p.lab_id = "lab"
        p.api_key = "key"
        p._do_poll()

        assert receipt_calls == []
        assert db.get_order(dropship_order["num"])["download_status"] == "ok"


# ── Hot-folder print-consumption detection ─────────────────────────────────────
# Landing in the hot folder only means DNP picked up the job — it does NOT mean
# it printed. An order should only flip to 'ready' once the file has actually
# disappeared from the folder (the printer consumed it), never just because the
# download succeeded. This is what a disconnected/out-of-paper printer exposed.

class TestCheckPendingPrints:
    def _setup_order_with_items(self, pickup_order, tmp_path, n=2):
        hot_folder = str(tmp_path / "Hot")
        os.makedirs(hot_folder, exist_ok=True)
        dest_id = db.upsert_destination("A", hot_folder, is_default=True)
        db.upsert_order(pickup_order)
        order = db.get_order(pickup_order["num"])
        item_ids = []
        for i in range(n):
            fname = f"img{i}.jpg"
            open(os.path.join(hot_folder, fname), "wb").close()  # simulate a real downloaded file
            item_ids.append(db.insert_order_item(order["id"], fname, "8x24", dest_id))
        return order, item_ids, hot_folder

    def test_file_still_present_stays_queued_no_ready_callback(self, pickup_order, tmp_path):
        order, item_ids, hot_folder = self._setup_order_with_items(pickup_order, tmp_path, n=1)
        ready_calls = []
        p = poller_module.Poller(on_order_ready=lambda num: ready_calls.append(num))

        p._check_pending_prints()

        assert db.get_order_items(pickup_order["num"])[0]["status"] == "queued"
        assert db.get_order(pickup_order["num"])["status"] == "received"
        assert ready_calls == []

    def test_file_consumed_marks_printed_and_fires_ready(self, pickup_order, tmp_path):
        order, item_ids, hot_folder = self._setup_order_with_items(pickup_order, tmp_path, n=1)
        os.remove(os.path.join(hot_folder, "img0.jpg"))  # simulate the printer consuming it
        ready_calls = []
        p = poller_module.Poller(on_order_ready=lambda num: ready_calls.append(num))

        p._check_pending_prints()

        assert db.get_order_items(pickup_order["num"])[0]["status"] == "printed"
        assert db.get_order(pickup_order["num"])["status"] == "ready"
        assert ready_calls == [pickup_order["num"]]

    def test_partial_consumption_does_not_promote_order_yet(self, pickup_order, tmp_path):
        order, item_ids, hot_folder = self._setup_order_with_items(pickup_order, tmp_path, n=2)
        os.remove(os.path.join(hot_folder, "img0.jpg"))  # only one of two consumed
        ready_calls = []
        p = poller_module.Poller(on_order_ready=lambda num: ready_calls.append(num))

        p._check_pending_prints()

        items = {i["filename"]: i["status"] for i in db.get_order_items(pickup_order["num"])}
        assert items["img0.jpg"] == "printed"
        assert items["img1.jpg"] == "queued"
        assert db.get_order(pickup_order["num"])["status"] == "received"
        assert ready_calls == []

    def test_no_pending_items_is_a_noop(self):
        p = poller_module.Poller()
        p._check_pending_prints()  # should not raise

    def test_destination_without_hot_folder_path_is_skipped_gracefully(self, pickup_order, tmp_path):
        db.upsert_order(pickup_order)
        order = db.get_order(pickup_order["num"])
        dest_id = db.upsert_destination("No Path", "")
        db.insert_order_item(order["id"], "img0.jpg", "8x24", dest_id)

        p = poller_module.Poller()
        p._check_pending_prints()  # should not raise

        assert db.get_order_items(pickup_order["num"])[0]["status"] == "queued"

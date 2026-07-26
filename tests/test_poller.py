"""
test_poller.py — Tests for order fulfillment mode classification.

The old global mode filter (_is_pickup_order, _order_matches_mode) was removed in
Phase 3.  Classification now happens inline in db.upsert_order() and is stored as
orders.fulfillment_mode.  These tests exercise that classification via the DB layer.
"""
import json
import os
import tempfile
import pytest

import db

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

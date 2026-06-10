"""
conftest.py — shared fixtures for PDX Onsite tests.

Every test that needs a database uses the `fresh_db` fixture,
which swaps out db.DB_PATH for a temp file and runs init_db().
Flask tests use the `client` fixture, which builds the app with
a MockPoller and the same isolated DB.
"""
import json
import os
import pytest

import db as _db
import config as _config


# ── Real sample orders from PhotoDay API ──────────────────────────────────────

PICKUP_ORDER = {
    "id": "3a1d6f0f-92d7-4438-9b4d-915638de931e",
    "num": "GS1777844776",
    "items": [
        {
            "id": "463e3c63-aea4-48a6-8151-ba332fb6a7c4",
            "images": [{"assetUrl": "https://photos-studio.photoday.io/order_item_products/print_ready_images/463/e3c/63-/original/40066_AN1A0007_1-ec808278.jpg?1777844924", "filename": "40066_AN1A0007_1-ec808278.jpg", "externalId": "8x24", "orientation": "vertical"}],
            "groupId": 1, "quantity": 1, "photoTags": ["Shining Stars Cheer_Bumble Bees"],
            "externalId": "combo2_8x24", "description": "2 Poster COMBO", "labCostCents": 1,
        },
    ],
    "groups": [],
    "studio": {"id": "45b4e9bc-c4a7-4bfc-a075-e60c337e2d0a", "name": "Jerry Hughes Photography LLC", "city": "LAS VEGAS", "state": "NV"},
    "gallery": "iNationals 2026",
    "placedAt": "2026-05-03T21:48:34.861Z",
    "shipping": {
        "option": {"id": "908bc997-3a0f-46f6-8cd9-e12e45e773f5", "name": "Pickup", "externalId": "pdx_pickup"},
        "return": {"city": "Bedford", "state": "TX", "recipient": "Jerry Hughes Photography"},
        "destination": {"city": "GALVESTON", "state": "TX", "zipCode": "77554", "recipient": "ZaTavia Taylor", "phone": "+12819749028"},
    },
    "isBulkOrder": False,
    "totalLabCostCents": 2,
}

DROPSHIP_ORDER = {
    "id": "46227815-3c8a-44ed-86f6-317bc359ff41",
    "num": "AD1779691289",
    "items": [
        {
            "id": "a4228401-93cf-45a5-bc48-ba8aa4d47e52",
            "images": [{"assetUrl": "https://photos-studio.photoday.io/order_item_products/print_ready_images/a42/284/01-/original/D0H_0368-cf42f0c3.jpg?1779691557", "filename": "D0H_0368-cf42f0c3.jpg", "externalId": "5x7", "orientation": "vertical"}],
            "groupId": 1, "quantity": 1, "photoTags": ["Handshake"],
            "externalId": "5x7_L|100914", "description": "5x7 Print", "labCostCents": 45,
        },
    ],
    "groups": [],
    "studio": {"id": "27c41304-f2c3-409f-97e4-18046db62476", "name": "BPI Photography Inc", "city": "TIFTON", "state": "GA"},
    "gallery": "Statesboro HS Graduation 25-26",
    "placedAt": "2026-05-25T06:45:50.082Z",
    "shipping": {
        "option": {"id": "b8c60a32-bb43-4749-bf04-88065eab9ead", "name": "Economy", "externalId": "economy"},
        "return": {"city": "Richmond", "state": "VA", "recipient": "Pro Photo Fulfillment"},
        "destination": {"city": "Statesboro", "state": "GA", "zipCode": "30461-7662", "recipient": "Brandi Shuman", "phone": "+19126014073"},
    },
    "isBulkOrder": False,
    "totalLabCostCents": 190,
}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Give each test a fresh isolated SQLite database."""
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(_db, "DB_PATH", db_file)
    _db.init_db()
    yield db_file


@pytest.fixture()
def pickup_order():
    return dict(PICKUP_ORDER)


@pytest.fixture()
def dropship_order():
    return dict(DROPSHIP_ORDER)


# ── Mock Poller ───────────────────────────────────────────────────────────────

class MockPoller:
    """Minimal poller stand-in for Flask app tests."""
    def __init__(self):
        self.running = False
        self.last_poll = None
        self.last_error = ""
        self.interval = 60
        self.next_poll_at = None
        self.on_new_orders = None
        self.on_poll_complete = None
        self.on_error = None
        self.on_download_done = None

    def configure(self, lab_id, api_key, interval):
        pass

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def trigger(self):
        pass

    def get_status(self):
        return {
            "running": self.running,
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "interval": self.interval,
            "next_poll_in": 0,
        }


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """Flask app wired to a temp DB and temp config file."""
    # Isolate DB
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(_db, "DB_PATH", db_file)
    _db.init_db()

    # Isolate config
    cfg_file = str(tmp_path / "test_config.json")
    monkeypatch.setattr(_config, "CONFIG_PATH", cfg_file)

    from server import create_app
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    flask_app = create_app(poller=MockPoller(), ui_path=os.path.join(project_root, "src"))
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture()
def client(app):
    return app.test_client()

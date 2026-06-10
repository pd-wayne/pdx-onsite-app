"""
test_poller.py — Tests for fulfillment mode filtering logic.

Uses the real shipping shapes from actual PhotoDay API responses.
"""
import pytest
from poller import _is_pickup_order, _order_matches_mode

# ── Minimal order shapes ──────────────────────────────────────────────────────

PICKUP = {"shipping": {"option": {"externalId": "pdx_pickup"}}}
ECONOMY = {"shipping": {"option": {"externalId": "economy"}}}
OVERNIGHT = {"shipping": {"option": {"externalId": "overnight"}}}
NO_SHIPPING = {}
MISSING_OPTION = {"shipping": {}}
MISSING_EXTERNAL_ID = {"shipping": {"option": {}}}


# ── _is_pickup_order ──────────────────────────────────────────────────────────

class TestIsPickupOrder:
    def test_pickup_externalid_returns_true(self):
        assert _is_pickup_order(PICKUP) is True

    def test_economy_externalid_returns_false(self):
        assert _is_pickup_order(ECONOMY) is False

    def test_overnight_externalid_returns_false(self):
        assert _is_pickup_order(OVERNIGHT) is False

    def test_missing_shipping_key_returns_false(self):
        assert _is_pickup_order(NO_SHIPPING) is False

    def test_missing_option_key_returns_false(self):
        assert _is_pickup_order(MISSING_OPTION) is False

    def test_missing_external_id_returns_false(self):
        assert _is_pickup_order(MISSING_EXTERNAL_ID) is False

    def test_real_pickup_order(self, pickup_order):
        assert _is_pickup_order(pickup_order) is True

    def test_real_dropship_order(self, dropship_order):
        assert _is_pickup_order(dropship_order) is False


# ── _order_matches_mode ───────────────────────────────────────────────────────

class TestOrderMatchesMode:
    # --- pickup mode ---
    def test_pickup_mode_accepts_pickup(self):
        assert _order_matches_mode(PICKUP, "pickup") is True

    def test_pickup_mode_rejects_economy(self):
        assert _order_matches_mode(ECONOMY, "pickup") is False

    def test_pickup_mode_rejects_overnight(self):
        assert _order_matches_mode(OVERNIGHT, "pickup") is False

    # --- dropship mode ---
    def test_dropship_mode_accepts_economy(self):
        assert _order_matches_mode(ECONOMY, "dropship") is True

    def test_dropship_mode_accepts_overnight(self):
        assert _order_matches_mode(OVERNIGHT, "dropship") is True

    def test_dropship_mode_rejects_pickup(self):
        assert _order_matches_mode(PICKUP, "dropship") is False

    # --- both mode ---
    def test_both_mode_accepts_pickup(self):
        assert _order_matches_mode(PICKUP, "both") is True

    def test_both_mode_accepts_economy(self):
        assert _order_matches_mode(ECONOMY, "both") is True

    def test_both_mode_accepts_overnight(self):
        assert _order_matches_mode(OVERNIGHT, "both") is True

    def test_both_mode_accepts_missing_shipping(self):
        assert _order_matches_mode(NO_SHIPPING, "both") is True

    # --- unknown mode defaults to pickup behaviour ---
    def test_unknown_mode_accepts_pickup(self):
        assert _order_matches_mode(PICKUP, "???") is True

    def test_unknown_mode_rejects_non_pickup(self):
        assert _order_matches_mode(ECONOMY, "???") is False

    # --- real order objects ---
    def test_real_pickup_in_pickup_mode(self, pickup_order):
        assert _order_matches_mode(pickup_order, "pickup") is True

    def test_real_dropship_in_pickup_mode(self, dropship_order):
        assert _order_matches_mode(dropship_order, "pickup") is False

    def test_real_pickup_in_dropship_mode(self, pickup_order):
        assert _order_matches_mode(pickup_order, "dropship") is False

    def test_real_dropship_in_dropship_mode(self, dropship_order):
        assert _order_matches_mode(dropship_order, "dropship") is True

    def test_real_pickup_in_both_mode(self, pickup_order):
        assert _order_matches_mode(pickup_order, "both") is True

    def test_real_dropship_in_both_mode(self, dropship_order):
        assert _order_matches_mode(dropship_order, "both") is True

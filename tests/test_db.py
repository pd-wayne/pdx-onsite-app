"""
test_db.py — Tests for database operations.

Covers upsert logic, gallery filtering on queue/history/stats/search,
status transitions, and the jobs table.
"""
import json
import pytest
import db


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_order(num, gallery, status="received", customer="Test Customer", shipping_type="pdx_pickup"):
    return {
        "num": num,
        "gallery": gallery,
        "status": status,
        "placedAt": "2026-01-01T00:00:00Z",
        "items": [],
        "shipping": {
            "option": {"externalId": shipping_type},
            "destination": {"recipient": customer},
        },
    }


# ── upsert_order ─────────────────────────────────────────────────────────────

class TestUpsertOrder:
    def test_new_order_returns_true(self, fresh_db, pickup_order):
        assert db.upsert_order(pickup_order) is True

    def test_duplicate_returns_false(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        assert db.upsert_order(pickup_order) is False

    def test_second_order_different_num_returns_true(self, fresh_db, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        assert db.upsert_order(dropship_order) is True

    def test_order_without_num_returns_false(self, fresh_db):
        assert db.upsert_order({"gallery": "Test"}) is False

    def test_customer_name_extracted_from_destination(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        order = db.get_order("GS1777844776")
        assert order["customer_name"] == "ZaTavia Taylor"

    def test_gallery_stored_correctly(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        order = db.get_order("GS1777844776")
        assert order["gallery"] == "iNationals 2026"

    def test_status_received_by_default(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        order = db.get_order("GS1777844776")
        assert order["status"] == "received"

    def test_pdx_fulfilled_status_stored_as_fulfilled(self, fresh_db):
        order = make_order("FULFILLED001", "Test Gallery", status="fulfilled")
        db.upsert_order(order)
        stored = db.get_order("FULFILLED001")
        assert stored["status"] == "fulfilled"

    def test_pdx_late_status_stored_as_received(self, fresh_db):
        order = make_order("LATE001", "Test Gallery", status="late")
        db.upsert_order(order)
        stored = db.get_order("LATE001")
        assert stored["status"] == "received"

    def test_pdx_unknown_status_stored_as_received(self, fresh_db):
        order = make_order("UNK001", "Test Gallery", status="something_weird")
        db.upsert_order(order)
        stored = db.get_order("UNK001")
        assert stored["status"] == "received"

    def test_duplicate_upsert_does_not_change_status(self, fresh_db):
        """Once an order is in the DB, polling cannot silently change its status.
        Only the scan/confirm flow (confirm_order) should move it to fulfilled."""
        order_recv = make_order("UPD001", "Test Gallery", status="received")
        db.upsert_order(order_recv)
        assert db.get_order("UPD001")["status"] == "received"

        # Even if PDX now reports it as fulfilled, upsert must not change it
        order_fulf = make_order("UPD001", "Test Gallery", status="fulfilled")
        db.upsert_order(order_fulf)
        assert db.get_order("UPD001")["status"] == "received"

    def test_images_stored_in_images_json(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        images = db.get_images_json("GS1777844776")
        assert isinstance(images, list)
        assert len(images) == 1
        assert images[0]["filename"] == "40066_AN1A0007_1-ec808278.jpg"

    def test_items_stored_in_items_json(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        order = db.get_order("GS1777844776")
        items = json.loads(order["items_json"])
        assert len(items) == 1
        assert items[0]["desc"] == "2 Poster COMBO"

    def test_creates_job_for_gallery(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        jobs = db.get_jobs()
        assert any(j["gallery"] == "iNationals 2026" for j in jobs)

    def test_no_job_created_for_empty_gallery(self, fresh_db):
        order = make_order("NOGA001", "")
        db.upsert_order(order)
        assert db.get_jobs() == []


# ── get_queue ─────────────────────────────────────────────────────────────────

class TestGetQueue:
    def test_empty_db_returns_empty(self, fresh_db):
        assert db.get_queue() == []

    def test_returns_received_order(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        queue = db.get_queue()
        assert len(queue) == 1
        assert queue[0]["order_num"] == "GS1777844776"

    def test_does_not_return_fulfilled_order(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        db.confirm_order("GS1777844776")
        assert db.get_queue() == []

    def test_gallery_filter_returns_matching_only(self, fresh_db, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)

        result = db.get_queue(gallery_filter="iNationals 2026")
        assert len(result) == 1
        assert result[0]["order_num"] == "GS1777844776"

    def test_gallery_filter_other_gallery(self, fresh_db, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)

        result = db.get_queue(gallery_filter="Statesboro HS Graduation 25-26")
        assert len(result) == 1
        assert result[0]["order_num"] == "AD1779691289"

    def test_gallery_filter_no_match_returns_empty(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        assert db.get_queue(gallery_filter="Nonexistent") == []

    def test_no_filter_returns_all(self, fresh_db, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)
        assert len(db.get_queue()) == 2

    def test_multiple_orders_same_gallery(self, fresh_db):
        for i in range(5):
            db.upsert_order(make_order(f"ORD{i:03d}", "Sports Day"))
        result = db.get_queue(gallery_filter="Sports Day")
        assert len(result) == 5

    def test_order_has_empty_items_list_when_none_created(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        result = db.get_queue()
        assert result[0]["items"] == []

    def test_order_items_attached_correctly(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        order = db.get_order("GS1777844776")
        db.seed_default_destination("/tmp/hotfolder")
        dest = db.get_default_destination()
        db.insert_order_item(order["id"], "a.jpg", "8x24", dest["id"])
        db.insert_order_item(order["id"], "b.jpg", "5x7", dest["id"])

        result = db.get_queue()
        items = result[0]["items"]
        assert len(items) == 2
        assert {i["print_spec"] for i in items} == {"8x24", "5x7"}
        assert all(i["status"] == "queued" for i in items)

    def test_items_not_cross_contaminated_between_orders(self, fresh_db, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)
        db.seed_default_destination("/tmp/hotfolder")
        dest = db.get_default_destination()

        pickup = db.get_order("GS1777844776")
        db.insert_order_item(pickup["id"], "a.jpg", "8x24", dest["id"])

        result = {o["order_num"]: o["items"] for o in db.get_queue()}
        assert len(result["GS1777844776"]) == 1
        assert result["AD1779691289"] == []


# ── get_history ───────────────────────────────────────────────────────────────

class TestGetHistory:
    def test_empty_db_returns_empty(self, fresh_db):
        assert db.get_history() == []

    def test_returns_fulfilled_order(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        db.confirm_order("GS1777844776")
        history = db.get_history()
        assert len(history) == 1
        assert history[0]["order_num"] == "GS1777844776"

    def test_does_not_return_received_order(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        assert db.get_history() == []

    def test_gallery_filter_returns_matching_only(self, fresh_db, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)
        db.confirm_order("GS1777844776")
        db.confirm_order("AD1779691289")

        result = db.get_history(gallery_filter="iNationals 2026")
        assert len(result) == 1
        assert result[0]["order_num"] == "GS1777844776"

    def test_gallery_filter_no_match_returns_empty(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        db.confirm_order("GS1777844776")
        assert db.get_history(gallery_filter="Nonexistent") == []


# ── get_stats ─────────────────────────────────────────────────────────────────

class TestGetStats:
    def test_empty_db_zeros(self, fresh_db):
        stats = db.get_stats()
        assert stats == {"total": 0, "pending": 0, "ready": 0, "confirmed": 0, "fulfilled": 0}

    def test_one_received_order(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        stats = db.get_stats()
        assert stats["total"] == 1
        assert stats["pending"] == 1
        assert stats["confirmed"] == 0

    def test_after_confirm_counts_update(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        db.confirm_order("GS1777844776")
        stats = db.get_stats()
        assert stats["pending"] == 0
        assert stats["confirmed"] == 1
        assert stats["total"] == 1

    def test_gallery_filter_only_counts_that_gallery(self, fresh_db, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)

        stats_a = db.get_stats(gallery_filter="iNationals 2026")
        assert stats_a["total"] == 1
        assert stats_a["pending"] == 1

        stats_b = db.get_stats(gallery_filter="Statesboro HS Graduation 25-26")
        assert stats_b["total"] == 1

    def test_gallery_filter_no_match_returns_zeros(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        stats = db.get_stats(gallery_filter="Nonexistent")
        assert stats["total"] == 0

    def test_global_stats_after_multiple_orders(self, fresh_db):
        for i in range(3):
            db.upsert_order(make_order(f"ORD{i:03d}", "Gallery A"))
        for i in range(2):
            db.upsert_order(make_order(f"ORD1{i:03d}", "Gallery B"))

        stats = db.get_stats()
        assert stats["total"] == 5
        assert stats["pending"] == 5


# ── search_orders ─────────────────────────────────────────────────────────────

class TestSearchOrders:
    def test_search_by_order_num(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        results = db.search_orders("GS1777844776")
        assert len(results) == 1

    def test_search_by_partial_order_num(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        results = db.search_orders("GS177")
        assert len(results) == 1

    def test_search_by_customer_name(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        results = db.search_orders("ZaTavia")
        assert len(results) == 1

    def test_search_no_match_returns_empty(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        results = db.search_orders("ZZZNOMATCH")
        assert results == []

    def test_search_gallery_filter_restricts_results(self, fresh_db, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)

        results = db.search_orders("2026", gallery_filter="iNationals 2026")
        assert all(r["gallery"] == "iNationals 2026" for r in results)

    def test_search_gallery_filter_finds_nothing_in_wrong_gallery(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        results = db.search_orders("ZaTavia", gallery_filter="Wrong Gallery")
        assert results == []


# ── confirm_order / set_fulfilled ─────────────────────────────────────────────

class TestOrderActions:
    def test_confirm_order_sets_status_fulfilled(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        result = db.confirm_order("GS1777844776")
        assert result is True
        assert db.get_order("GS1777844776")["status"] == "fulfilled"

    def test_confirm_nonexistent_order_returns_false(self, fresh_db):
        assert db.confirm_order("NOTEXIST") is False

    def test_confirm_already_fulfilled_returns_false(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        db.confirm_order("GS1777844776")
        assert db.confirm_order("GS1777844776") is False

    def test_set_fulfilled_updates_fulfill_status(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        result = db.set_fulfilled("GS1777844776")
        assert result is True
        order = db.get_order("GS1777844776")
        assert order["fulfill_status"] == "fulfilled"

    def test_set_fulfilled_leaves_status_received(self, fresh_db, pickup_order):
        """Images printed (fulfill_status=fulfilled) but customer hasn't confirmed (status=received)."""
        db.upsert_order(pickup_order)
        db.set_fulfilled("GS1777844776")
        order = db.get_order("GS1777844776")
        assert order["status"] == "received"        # still in queue

    def test_set_download_status_ok(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        db.set_download_status("GS1777844776", "ok")
        assert db.get_order("GS1777844776")["download_status"] == "ok"

    def test_set_download_status_failed_with_error(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        db.set_download_status("GS1777844776", "failed", "Connection timed out")
        order = db.get_order("GS1777844776")
        assert order["download_status"] == "failed"
        assert order["download_error"] == "Connection timed out"


# ── jobs table ────────────────────────────────────────────────────────────────

class TestJobs:
    def test_upsert_job_creates_entry(self, fresh_db):
        db.upsert_job("Spring Portraits")
        jobs = db.get_jobs()
        assert len(jobs) == 1
        assert jobs[0]["gallery"] == "Spring Portraits"

    def test_upsert_job_empty_gallery_ignored(self, fresh_db):
        db.upsert_job("")
        assert db.get_jobs() == []

    def test_upsert_job_twice_increments_count(self, fresh_db):
        db.upsert_job("Spring Portraits")
        db.upsert_job("Spring Portraits")
        jobs = db.get_jobs()
        assert len(jobs) == 1
        assert jobs[0]["order_count"] == 2

    def test_get_all_galleries_returns_distinct(self, fresh_db, pickup_order, dropship_order):
        db.upsert_order(pickup_order)
        db.upsert_order(dropship_order)
        galleries = db.get_all_galleries()
        assert "iNationals 2026" in galleries
        assert "Statesboro HS Graduation 25-26" in galleries
        assert len(set(galleries)) == len(galleries)   # no duplicates

    def test_get_all_galleries_empty_gallery_excluded(self, fresh_db):
        order = make_order("NOGA001", "")
        db.upsert_order(order)
        assert "" not in db.get_all_galleries()


# ── destinations ──────────────────────────────────────────────────────────────

class TestDestinations:
    def test_upsert_creates_new_destination(self, fresh_db):
        dest_id = db.upsert_destination("4x6 Printer", "C:\\Hot\\4x6")
        assert dest_id is not None
        dests = db.get_destinations()
        assert len(dests) == 1
        assert dests[0]["name"] == "4x6 Printer"
        assert dests[0]["hot_folder_path"] == "C:\\Hot\\4x6"
        assert dests[0]["active"] == 1

    def test_upsert_with_id_updates_existing(self, fresh_db):
        dest_id = db.upsert_destination("4x6 Printer", "C:\\Hot\\4x6")
        db.upsert_destination("4x6 Printer Renamed", "C:\\Hot\\4x6b", dest_id=dest_id)
        dests = db.get_destinations()
        assert len(dests) == 1
        assert dests[0]["id"] == dest_id
        assert dests[0]["name"] == "4x6 Printer Renamed"
        assert dests[0]["hot_folder_path"] == "C:\\Hot\\4x6b"

    def test_setting_new_default_clears_previous_default(self, fresh_db):
        first = db.upsert_destination("A", "C:\\A", is_default=True)
        second = db.upsert_destination("B", "C:\\B", is_default=True)
        dests = {d["id"]: d for d in db.get_destinations()}
        assert dests[first]["is_default"] == 0
        assert dests[second]["is_default"] == 1

    def test_get_default_destination_returns_marked_default(self, fresh_db):
        db.upsert_destination("A", "C:\\A", is_default=False)
        b = db.upsert_destination("B", "C:\\B", is_default=True)
        default = db.get_default_destination()
        assert default["id"] == b

    def test_get_default_destination_falls_back_to_any_active(self, fresh_db):
        dest_id = db.upsert_destination("A", "C:\\A", is_default=False)
        default = db.get_default_destination()
        assert default["id"] == dest_id

    def test_get_default_destination_none_when_empty(self, fresh_db):
        assert db.get_default_destination() is None

    def test_delete_destination_succeeds_when_unused(self, fresh_db):
        dest_id = db.upsert_destination("A", "C:\\A")
        assert db.delete_destination(dest_id) is True
        assert db.get_destinations() == []

    def test_delete_destination_blocked_when_referenced_by_order_item(self, fresh_db, pickup_order):
        dest_id = db.upsert_destination("A", "C:\\A")
        db.upsert_order(pickup_order)
        order = db.get_order("GS1777844776")
        db.insert_order_item(order["id"], "a.jpg", "8x24", dest_id)

        assert db.delete_destination(dest_id) is False
        assert len(db.get_destinations()) == 1

    def test_delete_destination_nulls_out_routing_references(self, fresh_db):
        dest_id = db.upsert_destination("A", "C:\\A")
        db.upsert_routing("8x24", dest_id)
        db.delete_destination(dest_id)
        routing = db.get_routing()
        assert routing[0]["destination_id"] is None

    def test_update_destination_health_sets_last_success_at(self, fresh_db):
        dest_id = db.upsert_destination("A", "C:\\A")
        assert db.get_destinations()[0]["last_success_at"] is None
        db.update_destination_health(dest_id)
        assert db.get_destinations()[0]["last_success_at"] is not None


# ── product routing ───────────────────────────────────────────────────────────

class TestProductRouting:
    def test_upsert_routing_creates_new_entry(self, fresh_db):
        dest_id = db.upsert_destination("A", "C:\\A")
        db.upsert_routing("8x24", dest_id)
        routing = db.get_routing()
        assert len(routing) == 1
        assert routing[0]["print_spec"] == "8x24"
        assert routing[0]["destination_id"] == dest_id

    def test_upsert_routing_updates_existing_spec(self, fresh_db):
        a = db.upsert_destination("A", "C:\\A")
        b = db.upsert_destination("B", "C:\\B")
        db.upsert_routing("8x24", a)
        db.upsert_routing("8x24", b)
        routing = db.get_routing()
        assert len(routing) == 1
        assert routing[0]["destination_id"] == b

    def test_upsert_routing_allows_unassigned(self, fresh_db):
        db.upsert_routing("8x24", None)
        assert db.get_routing()[0]["destination_id"] is None

    def test_discover_specs_adds_unknown_specs(self, fresh_db):
        added = db.discover_specs(["8x24", "5x7", ""])
        assert added == 2
        specs = {r["print_spec"] for r in db.get_routing()}
        assert specs == {"8x24", "5x7"}

    def test_discover_specs_skips_already_known(self, fresh_db):
        db.upsert_routing("8x24", None)
        added = db.discover_specs(["8x24", "5x7"])
        assert added == 1

    def test_get_destination_for_spec_returns_assigned_destination(self, fresh_db):
        default_dest = db.upsert_destination("Default", "C:\\Default", is_default=True)
        specific = db.upsert_destination("Poster", "C:\\Poster")
        db.upsert_routing("8x24", specific)
        resolved = db.get_destination_for_spec("8x24")
        assert resolved["id"] == specific

    def test_get_destination_for_spec_falls_back_to_default_when_unassigned(self, fresh_db):
        default_dest = db.upsert_destination("Default", "C:\\Default", is_default=True)
        db.upsert_routing("8x24", None)
        resolved = db.get_destination_for_spec("8x24")
        assert resolved["id"] == default_dest

    def test_get_destination_for_spec_falls_back_to_default_when_unknown_spec(self, fresh_db):
        default_dest = db.upsert_destination("Default", "C:\\Default", is_default=True)
        resolved = db.get_destination_for_spec("never-seen")
        assert resolved["id"] == default_dest

    def test_get_destination_for_spec_falls_back_when_assigned_destination_inactive(self, fresh_db):
        default_dest = db.upsert_destination("Default", "C:\\Default", is_default=True)
        inactive = db.upsert_destination("Old Printer", "C:\\Old", active=False)
        db.upsert_routing("8x24", inactive)
        resolved = db.get_destination_for_spec("8x24")
        assert resolved["id"] == default_dest


# ── order items / check_order_ready ────────────────────────────────────────────

class TestCheckOrderReady:
    def _setup_order_with_items(self, fresh_db, pickup_order, n=2):
        dest_id = db.upsert_destination("A", "C:\\A")
        db.upsert_order(pickup_order)
        order = db.get_order("GS1777844776")
        item_ids = [
            db.insert_order_item(order["id"], f"img{i}.jpg", "8x24", dest_id)
            for i in range(n)
        ]
        return order, item_ids

    def test_false_when_no_order_items_exist(self, fresh_db, pickup_order):
        db.upsert_order(pickup_order)
        assert db.check_order_ready("GS1777844776") is False
        assert db.get_order("GS1777844776")["status"] == "received"

    def test_false_when_some_items_still_queued(self, fresh_db, pickup_order):
        order, item_ids = self._setup_order_with_items(fresh_db, pickup_order, n=2)
        db.update_item_status(item_ids[0], "printed")
        assert db.check_order_ready("GS1777844776") is False
        assert db.get_order("GS1777844776")["status"] == "received"

    def test_true_and_transitions_when_all_items_printed(self, fresh_db, pickup_order):
        order, item_ids = self._setup_order_with_items(fresh_db, pickup_order, n=2)
        for item_id in item_ids:
            db.update_item_status(item_id, "printed")
        assert db.check_order_ready("GS1777844776") is True
        assert db.get_order("GS1777844776")["status"] == "ready"

    def test_error_status_item_blocks_ready(self, fresh_db, pickup_order):
        order, item_ids = self._setup_order_with_items(fresh_db, pickup_order, n=2)
        db.update_item_status(item_ids[0], "printed")
        db.update_item_status(item_ids[1], "error")
        assert db.check_order_ready("GS1777844776") is False
        assert db.get_order("GS1777844776")["status"] == "received"

    def test_false_when_already_ready(self, fresh_db, pickup_order):
        order, item_ids = self._setup_order_with_items(fresh_db, pickup_order, n=1)
        db.update_item_status(item_ids[0], "printed")
        assert db.check_order_ready("GS1777844776") is True
        # Second call: order is no longer 'received', so it should not re-fire
        assert db.check_order_ready("GS1777844776") is False

    def test_false_for_nonexistent_order(self, fresh_db):
        assert db.check_order_ready("NOPE") is False

    def test_update_item_status_sets_printed_at(self, fresh_db, pickup_order):
        order, item_ids = self._setup_order_with_items(fresh_db, pickup_order, n=1)
        before = db.get_order_items("GS1777844776")[0]
        assert before["printed_at"] is None
        db.update_item_status(item_ids[0], "printed")
        after = db.get_order_items("GS1777844776")[0]
        assert after["printed_at"] is not None
        assert after["status"] == "printed"


# ── get_pending_order_items ────────────────────────────────────────────────────
# Backs the hot-folder consumption check: only 'queued' items (still sitting in
# their hot folder, not yet confirmed printed) should surface here.

class TestGetPendingOrderItems:
    def _setup_order_with_items(self, fresh_db, pickup_order, n=2):
        dest_id = db.upsert_destination("A", "C:\\A")
        db.upsert_order(pickup_order)
        order = db.get_order("GS1777844776")
        item_ids = [
            db.insert_order_item(order["id"], f"img{i}.jpg", "8x24", dest_id)
            for i in range(n)
        ]
        return order, item_ids, dest_id

    def test_empty_when_no_order_items(self, fresh_db):
        assert db.get_pending_order_items() == []

    def test_returns_queued_items_with_order_num(self, fresh_db, pickup_order):
        order, item_ids, dest_id = self._setup_order_with_items(fresh_db, pickup_order, n=2)
        pending = db.get_pending_order_items()
        assert len(pending) == 2
        assert all(p["order_num"] == "GS1777844776" for p in pending)
        assert all(p["destination_id"] == dest_id for p in pending)
        assert {p["filename"] for p in pending} == {"img0.jpg", "img1.jpg"}

    def test_excludes_printed_items(self, fresh_db, pickup_order):
        order, item_ids, dest_id = self._setup_order_with_items(fresh_db, pickup_order, n=2)
        db.update_item_status(item_ids[0], "printed")
        pending = db.get_pending_order_items()
        assert len(pending) == 1
        assert pending[0]["filename"] == "img1.jpg"

    def test_excludes_error_items(self, fresh_db, pickup_order):
        order, item_ids, dest_id = self._setup_order_with_items(fresh_db, pickup_order, n=1)
        db.update_item_status(item_ids[0], "error")
        assert db.get_pending_order_items() == []

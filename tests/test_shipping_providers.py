"""
test_shipping_providers.py — Tests for the shipping-provider adapter layer.

Onsite creates the order in the provider at ingestion time and creates the
label on demand ("Ready to Ship") — these tests cover both calls plus the
carrier/service/package discovery calls used to build the Settings mapping UI.
"""
import pytest
import requests

import shipping_providers as sp


class _FakeResponse:
    def __init__(self, json_data, ok=True, status_code=200, text=""):
        self._json = json_data
        self.ok = ok
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json


class TestProviderCatalog:
    def test_includes_shipstation(self):
        catalog = sp.provider_catalog()
        types = {p["provider_type"] for p in catalog}
        assert "shipstation" in types

    def test_shipstation_declares_key_and_secret_fields(self):
        entry = next(p for p in sp.provider_catalog() if p["provider_type"] == "shipstation")
        keys = {f["key"] for f in entry["credential_fields"]}
        assert keys == {"api_key", "api_secret"}


class TestGetAdapter:
    def test_returns_shipstation_adapter(self):
        adapter = sp.get_adapter("shipstation", {"api_key": "k", "api_secret": "s"})
        assert isinstance(adapter, sp.ShipStationV1Adapter)

    def test_unknown_provider_type_raises(self):
        with pytest.raises(ValueError):
            sp.get_adapter("not_a_real_provider", {})


SAMPLE_ORDER = {
    "order_num": "PDX001",
    "placed_at": "2026-07-28T12:00:00Z",
    "studio_name": "Memory Makers Images",
    "destination": {
        "recipient": "Jane Doe", "address1": "123 Main St", "address2": "Apt 4",
        "city": "Orlando", "state": "FL", "zipCode": "32789", "country": "US", "phone": "4045551234",
    },
}


class TestShipStationV1CreateOrder:
    def test_missing_credentials_returns_error(self):
        adapter = sp.ShipStationV1Adapter({})
        external_id, err = adapter.create_order(SAMPLE_ORDER)
        assert external_id is None
        assert "required" in err.lower()

    def test_builds_bill_to_as_the_lab_not_the_customer(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"orderId": 555})
        monkeypatch.setattr(requests, "post", fake_post)

        external_id, err = adapter.create_order(SAMPLE_ORDER)
        assert err == ""
        assert external_id == "555"
        assert captured["billTo"]["name"] == "Memory Makers Images"
        assert captured["shipTo"]["name"] == "Jane Doe"
        assert captured["shipTo"]["street1"] == "123 Main St"
        assert captured["shipTo"]["postalCode"] == "32789"
        assert captured["orderNumber"] == "PDX001"
        assert captured["orderStatus"] == "awaiting_shipment"

    def test_missing_order_id_in_response_is_an_error(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse({}))
        external_id, err = adapter.create_order(SAMPLE_ORDER)
        assert external_id is None
        assert "orderId" in err

    def test_full_country_name_is_normalized_to_iso_code(self, monkeypatch):
        # Real case surfaced via the shipping-debug log: PDX sent "United
        # States" instead of "US", and ShipStation rejected the whole order
        # for it ("Please use a 2 character country code").
        order = {**SAMPLE_ORDER, "destination": {**SAMPLE_ORDER["destination"], "country": "United States"}}
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"orderId": 555})
        monkeypatch.setattr(requests, "post", fake_post)

        external_id, err = adapter.create_order(order)
        assert err == ""
        assert captured["shipTo"]["country"] == "US"

    def test_http_error_returns_message(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse({}, ok=False, status_code=422, text="Bad address"))
        external_id, err = adapter.create_order(SAMPLE_ORDER)
        assert external_id is None
        assert "422" in err

    def test_connection_error_returns_message(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        def raise_conn_error(*a, **k):
            raise requests.exceptions.ConnectionError()
        monkeypatch.setattr(requests, "post", raise_conn_error)
        external_id, err = adapter.create_order(SAMPLE_ORDER)
        assert external_id is None
        assert "connection" in err.lower()


class TestShipStationV1CreateLabel:
    def test_missing_credentials_returns_error(self):
        adapter = sp.ShipStationV1Adapter({})
        result, err = adapter.create_label("555", "ups", "ups_ground", "package", "none", "2026-07-28", 0.1)
        assert result is None
        assert "required" in err.lower()

    def test_sends_required_fields_and_omits_package_code_when_blank(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"trackingNumber": "1Z999", "shipmentCost": 8.5, "labelData": "base64=="})
        monkeypatch.setattr(requests, "post", fake_post)

        result, err = adapter.create_label("555", "ups", "ups_ground", "", "none", "2026-07-28", 0.1)
        assert err == ""
        assert result["tracking_number"] == "1Z999"
        assert captured["orderId"] == 555
        assert captured["carrierCode"] == "ups"
        assert captured["serviceCode"] == "ups_ground"
        assert captured["confirmation"] == "none"
        assert captured["shipDate"] == "2026-07-28"
        assert captured["testLabel"] is False
        assert captured["weight"] == {"value": 0.1, "units": "pounds"}
        assert "packageCode" not in captured

    def test_includes_package_code_when_provided(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"trackingNumber": "1Z999"})
        monkeypatch.setattr(requests, "post", fake_post)

        adapter.create_label("555", "stamps_com", "usps_priority_mail", "large_flat_rate_box", "none", "2026-07-28", 0.1)
        assert captured["packageCode"] == "large_flat_rate_box"

    def test_test_label_flag_passed_through(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"trackingNumber": "TEST123"})
        monkeypatch.setattr(requests, "post", fake_post)

        adapter.create_label("555", "stamps_com", "usps_priority_mail", "", "none", "2026-07-28", 0.1, test_label=True)
        assert captured["testLabel"] is True

    def test_weight_reflects_requested_value(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"trackingNumber": "1Z999"})
        monkeypatch.setattr(requests, "post", fake_post)

        adapter.create_label("555", "ups", "ups_ground", "", "none", "2026-07-28", 2.5)
        assert captured["weight"] == {"value": 2.5, "units": "pounds"}

    def test_result_includes_shipment_id_for_later_voiding(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(
            {"trackingNumber": "1Z999", "shipmentId": 443105328}))
        result, err = adapter.create_label("555", "ups", "ups_ground", "", "none", "2026-07-28", 0.1)
        assert err == ""
        assert result["shipment_id"] == 443105328


class TestShipStationV1VoidLabel:
    """ShipStation's own documented way to test a walleted carrier (which
    never supports testLabel=true): buy a real label, then void it — the
    walleted balance is refunded, usually right away."""

    def test_missing_credentials_returns_error(self):
        adapter = sp.ShipStationV1Adapter({})
        voided, err = adapter.void_label(443105328)
        assert voided is False
        assert "required" in err.lower()

    def test_approved_void_is_success(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"approved": True, "message": "Label voided successfully"})
        monkeypatch.setattr(requests, "post", fake_post)

        voided, err = adapter.void_label(443105328)
        assert voided is True
        assert err == ""
        assert captured == {"shipmentId": 443105328}

    def test_unapproved_void_is_reported(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(
            {"approved": False, "message": "Label already used"}))
        voided, err = adapter.void_label(443105328)
        assert voided is False
        assert err == "Label already used"

    def test_http_error_is_reported(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse(
            {}, ok=False, status_code=500, text="server error"))
        voided, err = adapter.void_label(443105328)
        assert voided is False
        assert err != ""

    def test_http_error_returns_message(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse({}, ok=False, status_code=500, text="Server error"))
        result, err = adapter.create_label("555", "ups", "ups_ground", "", "none", "2026-07-28", 0.1)
        assert result is None
        assert "500" in err

    def test_validation_error_surfaces_exception_message_not_raw_json(self, monkeypatch):
        """Real response body seen from ShipStation V1 when a service that
        requires a package type (e.g. stamps_com/usps_priority_mail) doesn't get
        one — verified against the live API. Staff should see the plain message,
        not the raw JSON/stack trace envelope."""
        real_error_body = {
            "Message": "An error has occurred.",
            "ExceptionMessage": "No package type has been selected.",
            "ExceptionType": "SS.Core.Objects.Exceptions.OrderValidationException",
            "StackTrace": "   at SS.Core.Objects.Shipments.<>c__DisplayClass83_1.<CreateLabel>b__0(ValidationMessage message)",
        }
        monkeypatch.setattr(requests, "post",
                           lambda *a, **k: _FakeResponse(real_error_body, ok=False, status_code=500, text=str(real_error_body)))
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        result, err = adapter.create_label("555", "stamps_com", "usps_priority_mail", "", "none", "2026-07-28", 0.1)
        assert result is None
        assert err == "ShipStation error: No package type has been selected."


class TestNormalizeCountry:
    def test_full_name_maps_to_code(self):
        assert sp._normalize_country("United States") == "US"
        assert sp._normalize_country("united states") == "US"
        assert sp._normalize_country("Canada") == "CA"

    def test_already_a_code_passes_through_uppercased(self):
        assert sp._normalize_country("us") == "US"
        assert sp._normalize_country("GB") == "GB"

    def test_unrecognized_name_falls_back_to_us(self):
        assert sp._normalize_country("Wakanda") == "US"

    def test_missing_falls_back_to_us(self):
        assert sp._normalize_country("") == "US"
        assert sp._normalize_country(None) == "US"


class TestShippingDebugLog:
    """A small, capped record of failed ShipStation calls — the exact
    request/response, not just a one-line message — so a real error is
    diagnosable without hunting through the full Activity Log."""

    def setup_method(self):
        sp._debug_log.clear()

    def test_failed_call_is_recorded(self, monkeypatch):
        monkeypatch.setattr(requests, "post",
                           lambda *a, **k: _FakeResponse({}, ok=False, status_code=422, text="Bad address"))
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        adapter.create_order(SAMPLE_ORDER)

        entries = sp.get_debug_log()
        assert len(entries) == 1
        assert entries[0]["path"] == "/orders/createorder"
        assert entries[0]["request"]["orderNumber"] == "PDX001"
        assert "422" in entries[0]["error"]

    def test_successful_call_is_not_recorded(self, monkeypatch):
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse({"orderId": 555}))
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        adapter.create_order(SAMPLE_ORDER)
        assert sp.get_debug_log() == []

    def test_connection_error_is_recorded(self, monkeypatch):
        def raise_conn_error(*a, **k):
            raise requests.exceptions.ConnectionError()
        monkeypatch.setattr(requests, "post", raise_conn_error)
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        adapter.create_order(SAMPLE_ORDER)

        entries = sp.get_debug_log()
        assert len(entries) == 1
        assert "connection" in entries[0]["error"].lower()

    def test_log_stays_capped(self, monkeypatch):
        monkeypatch.setattr(requests, "post",
                           lambda *a, **k: _FakeResponse({}, ok=False, status_code=500, text="fail"))
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        for _ in range(10):
            adapter.create_order(SAMPLE_ORDER)
        assert len(sp.get_debug_log()) == sp._debug_log.maxlen


class TestShipStationV1Discovery:
    def test_list_carriers(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(
            [{"code": "stamps_com", "name": "Stamps.com"}, {"code": "ups_walleted", "name": "UPS"}]
        ))
        carriers, err = adapter.list_carriers()
        assert err == ""
        assert carriers == [{"code": "stamps_com", "name": "Stamps.com"}, {"code": "ups_walleted", "name": "UPS"}]

    def test_list_carriers_missing_credentials(self):
        adapter = sp.ShipStationV1Adapter({})
        carriers, err = adapter.list_carriers()
        assert carriers == []
        assert "required" in err.lower()

    def test_list_services_passes_carrier_code(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_get(url, auth, params, timeout):
            captured.update(params)
            return _FakeResponse([{"code": "ups_ground", "name": "UPS Ground"}])
        monkeypatch.setattr(requests, "get", fake_get)

        services, err = adapter.list_services("ups_walleted")
        assert err == ""
        assert services == [{"code": "ups_ground", "name": "UPS Ground"}]
        assert captured["carrierCode"] == "ups_walleted"

    def test_list_packages_includes_flat_rate_types(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse([
            {"code": "large_flat_rate_box", "name": "Large Flat Rate Box"},
            {"code": "package", "name": "Package"},
        ]))
        packages, err = adapter.list_packages("stamps_com")
        assert err == ""
        assert {"code": "large_flat_rate_box", "name": "Large Flat Rate Box"} in packages

    def test_connection_error_returns_message(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        def raise_conn_error(*a, **k):
            raise requests.exceptions.ConnectionError()
        monkeypatch.setattr(requests, "get", raise_conn_error)
        carriers, err = adapter.list_carriers()
        assert carriers == []
        assert "connection" in err.lower()

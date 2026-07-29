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
        result, err = adapter.create_label("555", "ups", "ups_ground", "package", "none", "2026-07-28")
        assert result is None
        assert "required" in err.lower()

    def test_sends_required_fields_and_omits_package_code_when_blank(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"trackingNumber": "1Z999", "shipmentCost": 8.5, "labelData": "base64=="})
        monkeypatch.setattr(requests, "post", fake_post)

        result, err = adapter.create_label("555", "ups", "ups_ground", "", "none", "2026-07-28")
        assert err == ""
        assert result["tracking_number"] == "1Z999"
        assert captured["orderId"] == 555
        assert captured["carrierCode"] == "ups"
        assert captured["serviceCode"] == "ups_ground"
        assert captured["confirmation"] == "none"
        assert captured["shipDate"] == "2026-07-28"
        assert captured["testLabel"] is False
        assert "packageCode" not in captured

    def test_includes_package_code_when_provided(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"trackingNumber": "1Z999"})
        monkeypatch.setattr(requests, "post", fake_post)

        adapter.create_label("555", "stamps_com", "usps_priority_mail", "large_flat_rate_box", "none", "2026-07-28")
        assert captured["packageCode"] == "large_flat_rate_box"

    def test_test_label_flag_passed_through(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        captured = {}
        def fake_post(url, auth, json, timeout):
            captured.update(json)
            return _FakeResponse({"trackingNumber": "TEST123"})
        monkeypatch.setattr(requests, "post", fake_post)

        adapter.create_label("555", "stamps_com", "usps_priority_mail", "", "none", "2026-07-28", test_label=True)
        assert captured["testLabel"] is True

    def test_http_error_returns_message(self, monkeypatch):
        adapter = sp.ShipStationV1Adapter({"api_key": "k", "api_secret": "s"})
        monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponse({}, ok=False, status_code=500, text="Server error"))
        result, err = adapter.create_label("555", "ups", "ups_ground", "", "none", "2026-07-28")
        assert result is None
        assert "500" in err


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

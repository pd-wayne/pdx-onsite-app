"""
shipping_providers.py — pluggable shipping-label provider adapters (ShipStation, etc.)

Onsite creates the order in the provider at ingestion time (see poller.py) and
creates the label on demand when staff click "Ready to Ship" (see server.py) —
there's no discovery polling, Onsite is the source pushing data in.

Each adapter implements a common interface so the rest of the app never needs
provider-specific logic. Add a new provider by writing one adapter class and
registering it in PROVIDER_ADAPTERS — nothing else needs to change.
"""
import logging

import requests

log = logging.getLogger("pdx.shipping")


class ShippingProviderAdapter:
    """Base interface every shipping-label provider adapter must implement."""
    provider_type = "base"
    display_name = "Base Provider"
    credential_fields = []  # [{"key": "...", "label": "...", "secret": bool}, ...]

    def __init__(self, credentials: dict):
        self.credentials = credentials or {}

    def create_order(self, order: dict) -> tuple:
        """order: {order_num, placed_at, studio_name, destination: {recipient,
        address1, address2, city, state, zipCode, country, phone}}.
        Returns (external_order_id, error)."""
        raise NotImplementedError

    def create_label(self, external_order_id: str, carrier_code: str, service_code: str,
                     package_code: str, confirmation: str, ship_date: str,
                     test_label: bool = False) -> tuple:
        """Returns (result, error). result: {tracking_number, shipment_cost, label_data}."""
        raise NotImplementedError

    def list_carriers(self) -> tuple:
        """Returns ([{code, name}, ...], error) — carriers connected to this account."""
        raise NotImplementedError

    def list_services(self, carrier_code: str) -> tuple:
        """Returns ([{code, name}, ...], error) — services available for a carrier."""
        raise NotImplementedError

    def list_packages(self, carrier_code: str) -> tuple:
        """Returns ([{code, name}, ...], error) — package types for a carrier,
        including flat-rate options where the carrier offers them."""
        raise NotImplementedError


class ShipStationV1Adapter(ShippingProviderAdapter):
    """ShipStation V1 — chosen over V2 for this integration because V1's Orders
    concept is what we push into directly (we control orderNumber ourselves),
    whereas V2 is shipment/label-first and assumes something else already fed
    it order data."""
    provider_type = "shipstation"
    display_name = "ShipStation"
    credential_fields = [
        {"key": "api_key", "label": "API Key", "secret": False},
        {"key": "api_secret", "label": "API Secret", "secret": True},
    ]
    BASE_URL = "https://ssapi.shipstation.com"

    def _auth(self):
        return (self.credentials.get("api_key", ""), self.credentials.get("api_secret", ""))

    def _get(self, path: str, params: dict):
        api_key, api_secret = self._auth()
        if not api_key or not api_secret:
            return None, "ShipStation API Key and Secret are required"
        try:
            resp = requests.get(f"{self.BASE_URL}{path}", auth=(api_key, api_secret), params=params, timeout=20)
        except requests.exceptions.ConnectionError:
            return None, "Connection error"
        except requests.exceptions.Timeout:
            return None, "Request timed out"
        except Exception as e:
            return None, str(e)
        if not resp.ok:
            return None, f"HTTP {resp.status_code}: {resp.text[:300]}"
        return resp.json(), ""

    def _post(self, path: str, body: dict):
        api_key, api_secret = self._auth()
        if not api_key or not api_secret:
            return None, "ShipStation API Key and Secret are required"
        try:
            resp = requests.post(f"{self.BASE_URL}{path}", auth=(api_key, api_secret), json=body, timeout=30)
        except requests.exceptions.ConnectionError:
            return None, "Connection error"
        except requests.exceptions.Timeout:
            return None, "Request timed out"
        except Exception as e:
            return None, str(e)
        if not resp.ok:
            return None, f"HTTP {resp.status_code}: {resp.text[:300]}"
        return resp.json(), ""

    def create_order(self, order: dict) -> tuple:
        dest = order.get("destination", {}) or {}
        body = {
            "orderNumber": order["order_num"],
            "orderDate": order.get("placed_at", ""),
            "orderStatus": "awaiting_shipment",
            # billTo is the lab, not the customer — the customer already paid
            # for shipping before the order ever reaches PDX.
            "billTo": {"name": order.get("studio_name", "") or "Studio"},
            "shipTo": {
                "name": dest.get("recipient", ""),
                "street1": dest.get("address1", ""),
                "street2": dest.get("address2", ""),
                "city": dest.get("city", ""),
                "state": dest.get("state", ""),
                "postalCode": dest.get("zipCode", ""),
                "country": dest.get("country", "US"),
                "phone": dest.get("phone", ""),
            },
        }
        data, err = self._post("/orders/createorder", body)
        if err:
            return None, err
        external_order_id = data.get("orderId")
        if external_order_id is None:
            return None, "ShipStation did not return an orderId"
        return str(external_order_id), ""

    def create_label(self, external_order_id: str, carrier_code: str, service_code: str,
                     package_code: str, confirmation: str, ship_date: str,
                     test_label: bool = False) -> tuple:
        body = {
            "orderId": int(external_order_id),
            "carrierCode": carrier_code,
            "serviceCode": service_code,
            "confirmation": confirmation or "none",
            "shipDate": ship_date,
            "testLabel": test_label,
        }
        if package_code:
            body["packageCode"] = package_code
        data, err = self._post("/orders/createlabelfororder", body)
        if err:
            return None, err
        return {
            "tracking_number": data.get("trackingNumber", ""),
            "shipment_cost": data.get("shipmentCost"),
            "label_data": data.get("labelData", ""),
        }, ""

    def list_carriers(self) -> tuple:
        data, err = self._get("/carriers", {})
        if err:
            return [], err
        return [{"code": c.get("code", ""), "name": c.get("name", "")} for c in data if c.get("code")], ""

    def list_services(self, carrier_code: str) -> tuple:
        data, err = self._get("/carriers/listservices", {"carrierCode": carrier_code})
        if err:
            return [], err
        return [{"code": s.get("code", ""), "name": s.get("name", "")} for s in data if s.get("code")], ""

    def list_packages(self, carrier_code: str) -> tuple:
        data, err = self._get("/carriers/listpackages", {"carrierCode": carrier_code})
        if err:
            return [], err
        return [{"code": p.get("code", ""), "name": p.get("name", "")} for p in data if p.get("code")], ""


PROVIDER_ADAPTERS = {
    ShipStationV1Adapter.provider_type: ShipStationV1Adapter,
}


def get_adapter(provider_type: str, credentials: dict) -> ShippingProviderAdapter:
    cls = PROVIDER_ADAPTERS.get(provider_type)
    if not cls:
        raise ValueError(f"Unknown shipping provider type: {provider_type}")
    return cls(credentials)


def provider_catalog() -> list:
    """For the Settings UI — every available provider type and the credential
    fields it needs, so the "add provider" form can render itself generically
    instead of hardcoding one provider's field shape."""
    return [
        {
            "provider_type": cls.provider_type,
            "display_name": cls.display_name,
            "credential_fields": cls.credential_fields,
        }
        for cls in PROVIDER_ADAPTERS.values()
    ]

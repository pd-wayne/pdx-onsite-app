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
from collections import deque
from datetime import datetime

import requests

log = logging.getLogger("pdx.shipping")

# A small, dedicated record of the last few failed ShipStation calls — the
# exact request body and response, not just a one-line error string. Kept
# separate from the main Activity Log on purpose: that log is meant for
# studio staff and grows unbounded, which makes it a bad place to hunt for
# one technical error and a worse thing to paste into a support request.
# This stays capped and small enough to copy/paste directly. See
# /api/shipping_debug_log in server.py and the "Last Shipping Errors" button
# in Settings → Shipping Providers.
_debug_log: deque = deque(maxlen=6)


def get_debug_log() -> list:
    return list(_debug_log)


def _record_failure(path: str, request_body: dict, error: str, response_text: str = ""):
    _debug_log.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "path": path,
        "request": request_body,
        "error": error,
        "response": response_text[:500] if response_text else "",
    })


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
                     weight_lb: float, test_label: bool = False) -> tuple:
        """Returns (result, error). result: {tracking_number, shipment_cost,
        label_data, shipment_id} — shipment_id is what void_label takes."""
        raise NotImplementedError

    def void_label(self, shipment_id) -> tuple:
        """Voids a label bought in error or purely to test — refunds a
        walleted carrier's balance (usually instantly; the carrier's own
        policy governs the exact timing). Returns (voided: bool, error)."""
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


# ShipStation requires a 2-letter ISO country code and rejects anything else
# outright ("Please use a 2 character country code") — but PDX destinations
# aren't guaranteed to already be codes (a real order surfaced this via the
# new shipping-debug log: "United States" came through as the full name).
# Covers the common full names a US-based studio's orders are realistically
# going to see; anything unrecognized falls back to "US" rather than
# guessing wrong, same as the pre-existing default for a missing country.
_COUNTRY_CODES = {
    "united states": "US", "united states of america": "US", "usa": "US", "us": "US",
    "canada": "CA", "mexico": "MX",
    "united kingdom": "GB", "great britain": "GB", "uk": "GB",
    "australia": "AU", "new zealand": "NZ",
}


def _normalize_country(value: str) -> str:
    value = (value or "").strip()
    if len(value) == 2:
        return value.upper()
    return _COUNTRY_CODES.get(value.lower(), "US")


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

    def _format_error(self, resp) -> str:
        """ShipStation V1 wraps validation failures (bad weight, missing package
        type, bad address, etc.) in a JSON body with an ExceptionMessage — surface
        that directly instead of the raw HTTP status + JSON blob, so staff (and
        Settings error toasts) see something actionable."""
        try:
            body = resp.json()
            msg = body.get("ExceptionMessage") or body.get("Message")
            if msg:
                return f"ShipStation error: {msg}"
        except Exception:
            pass
        return f"HTTP {resp.status_code}: {resp.text[:300]}"

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
            return None, self._format_error(resp)
        return resp.json(), ""

    def _post(self, path: str, body: dict):
        api_key, api_secret = self._auth()
        if not api_key or not api_secret:
            return None, "ShipStation API Key and Secret are required"
        try:
            resp = requests.post(f"{self.BASE_URL}{path}", auth=(api_key, api_secret), json=body, timeout=30)
        except requests.exceptions.ConnectionError:
            _record_failure(path, body, "Connection error")
            return None, "Connection error"
        except requests.exceptions.Timeout:
            _record_failure(path, body, "Request timed out")
            return None, "Request timed out"
        except Exception as e:
            _record_failure(path, body, str(e))
            return None, str(e)
        if not resp.ok:
            err = self._format_error(resp)
            _record_failure(path, body, err, resp.text)
            return None, err
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
                "country": _normalize_country(dest.get("country", "US")),
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
                     weight_lb: float, test_label: bool = False) -> tuple:
        body = {
            "orderId": int(external_order_id),
            "carrierCode": carrier_code,
            "serviceCode": service_code,
            "confirmation": confirmation or "none",
            "shipDate": ship_date,
            "testLabel": test_label,
            "weight": {"value": weight_lb, "units": "pounds"},
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
            "shipment_id": data.get("shipmentId"),
        }, ""

    def void_label(self, shipment_id) -> tuple:
        data, err = self._post("/shipments/voidlabel", {"shipmentId": int(shipment_id)})
        if err:
            return False, err
        if data.get("approved"):
            return True, ""
        return False, data.get("message", "Void request was not approved")

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

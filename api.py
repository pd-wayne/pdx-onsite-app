"""
api.py — PDX API calls
"""
import requests
import logging

log = logging.getLogger("pdx.api")
BASE_URL = "https://api.photoday.io"


def _get_orders(lab_id: str, api_key: str, status: str, limit: int = 50, page: int = 1) -> tuple[list, str]:
    url = f"{BASE_URL}/pdx/{lab_id}/integrations/orders"
    params = {"status": status, "limit": limit, "page": page}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.ok:
            data = resp.json()
            if isinstance(data, list):
                return data, ""
            elif isinstance(data, dict):
                return data.get("orders", data.get("data", [])), ""
            return [], ""
        return [], f"HTTP {resp.status_code}: {resp.text[:200]}"
    except requests.exceptions.ConnectionError:
        return [], "Connection error"
    except requests.exceptions.Timeout:
        return [], "Request timed out"
    except Exception as e:
        return [], str(e)


def poll_orders(lab_id: str, api_key: str) -> tuple[list, str]:
    """Fetch received orders for live polling."""
    if not lab_id or not api_key:
        return [], "Lab ID and API key are required"
    orders, err = _get_orders(lab_id, api_key, "received", limit=50)
    if not err:
        log.info(f"[Poll] Fetched {len(orders)} received orders")
    return orders, err


def fetch_all_orders_for_job(lab_id: str, api_key: str, gallery: str) -> tuple[list, str]:
    """
    Fetch all orders for a specific gallery/job across all statuses.
    Used when a job is selected to populate full history.
    """
    if not lab_id or not api_key:
        return [], "Lab ID and API key are required"

    all_orders = []
    statuses = ["received", "late", "fulfilled"]

    for status in statuses:
        page = 1
        while True:
            orders, err = _get_orders(lab_id, api_key, status, limit=50, page=page)
            if err or not orders:
                break
            # Filter to this gallery
            matching = [o for o in orders if o.get("gallery", "") == gallery]
            all_orders.extend(matching)
            # If we got fewer than 50, no more pages
            if len(orders) < 50:
                break
            page += 1

    log.info(f"[API] Fetched {len(all_orders)} orders for job '{gallery}'")
    return all_orders, ""


def fetch_historical_orders(lab_id: str, api_key: str, limit_per_status: int = 200) -> tuple[list, str]:
    """
    Fetch historical orders across statuses to build the jobs list.
    Called on startup and when credentials are saved.
    """
    if not lab_id or not api_key:
        return [], "Lab ID and API key are required"

    all_orders = []
    # PDX only has three statuses: received, late, fulfilled
    statuses = ["received", "late", "fulfilled"]

    for status in statuses:
        page = 1
        fetched = 0
        while fetched < limit_per_status:
            orders, err = _get_orders(lab_id, api_key, status, limit=50, page=page)
            if err or not orders:
                break
            all_orders.extend(orders)
            fetched += len(orders)
            if len(orders) < 50:
                break
            page += 1

    log.info(f"[API] Fetched {len(all_orders)} historical orders for jobs list")
    return all_orders, ""


def shipped_callback(lab_id: str, api_key: str, order_num: str,
                     carrier: str = "Pickup", tracking_number: str = "") -> tuple[bool, str]:
    """Tell PDX an order has shipped — same endpoint whether it's an in-person
    pickup (carrier="Pickup", no tracking number) or a real shipment (carrier +
    tracking number from the studio's shipping process)."""
    if not lab_id or not api_key:
        return False, "Lab ID and API key are required"
    url = f"{BASE_URL}/pdx/{lab_id}/integrations/orders/{order_num}/shipped"
    payload = {"carrier": carrier, "trackingNumber": tracking_number}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.ok:
            log.info(f"[Callback] Shipped: {order_num}")
            return True, ""
        msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
        log.warning(f"[Callback] Failed for {order_num}: {msg}")
        return False, msg
    except requests.exceptions.ConnectionError:
        return False, "Connection error"
    except requests.exceptions.Timeout:
        return False, "Request timed out"
    except Exception as e:
        return False, str(e)


def test_connection(lab_id: str, api_key: str) -> tuple[bool, str]:
    orders, err = poll_orders(lab_id, api_key)
    if err:
        return False, err
    return True, f"Connected — {len(orders)} pending order(s) found"

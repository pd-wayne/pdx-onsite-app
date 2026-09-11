"""
discovery.py — LAN discovery for the same-location multi-station workflow.

Plain UDP broadcast/reply, no new dependencies (no zeroconf/mDNS). A primary
station runs a DiscoveryResponder that answers "who's out there?" broadcasts
with its name and address; a station setting up as a second station calls
discover_stations() to collect those replies and show plain station names —
never a raw IP.

This deliberately does NOT try to survive networks with client isolation
(some venue guest WiFi blocks device-to-device traffic entirely, broadcast
included) — that's a real, separate risk flagged to the user, not something
a discovery protocol can work around. A manual-entry fallback stays available
in the UI for exactly that case.
"""
import json
import logging
import socket
import threading
import time

log = logging.getLogger("pdx.discovery")

PORT = 50505
MAGIC = "pdxonsite"           # cheap sanity check so we ignore stray UDP traffic
REQUEST_TYPE = "discover"
REPLY_TYPE = "announce"


def _make_broadcast_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return s


class DiscoveryResponder:
    """Runs only while this station is a primary. Listens for discovery
    broadcasts and replies directly to whoever asked, with this station's
    name and address — so a second station never needs to know an IP."""

    def __init__(self, get_info):
        # get_info() -> dict — called fresh on every reply so a renamed
        # station or changed LAN IP is always reflected, not stale.
        self._get_info = get_info
        self._sock: socket.socket = None
        self._thread: threading.Thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info("[Discovery] Responder started")

    def stop(self):
        self._stop_event.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        log.info("[Discovery] Responder stopped")

    def _run(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("", PORT))
            self._sock.settimeout(1.0)
        except OSError as e:
            log.warning(f"[Discovery] Could not bind UDP port {PORT}: {e}")
            return

        while not self._stop_event.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("magic") != MAGIC or msg.get("type") != REQUEST_TYPE:
                continue
            try:
                info = self._get_info() or {}
                reply = json.dumps({
                    "magic": MAGIC, "type": REPLY_TYPE,
                    "name": info.get("name", ""),
                    "url": info.get("url", ""),
                    "studio_name": info.get("studio_name", ""),
                })
                self._sock.sendto(reply.encode("utf-8"), addr)
            except Exception as e:
                log.warning(f"[Discovery] Reply failed: {e}")


def discover_stations(timeout: float = 2.5, target: str = "<broadcast>") -> list:
    """Broadcast a discovery request and collect replies for `timeout`
    seconds. Returns a de-duplicated (by url) list of
    {name, url, studio_name} dicts — empty if nothing answers (blocked
    network, or no primary running).

    `target` defaults to a real LAN broadcast; tests pass "127.0.0.1" to
    exercise the actual wire protocol over loopback unicast instead, since
    broadcast routing isn't reliably available in sandboxed/CI environments
    (real venue LANs don't have this problem)."""
    found = {}
    try:
        s = _make_broadcast_socket()
        s.settimeout(0.3)
        request = json.dumps({"magic": MAGIC, "type": REQUEST_TYPE}).encode("utf-8")
        deadline = time.monotonic() + timeout
        # Re-send a few times across the window — a single broadcast can be
        # dropped, and this costs nothing on a LAN.
        next_send = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_send:
                try:
                    s.sendto(request, (target, PORT))
                except OSError as e:
                    log.warning(f"[Discovery] Broadcast send failed: {e}")
                    break
                next_send = time.monotonic() + 0.6
            try:
                data, _addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("magic") != MAGIC or msg.get("type") != REPLY_TYPE:
                continue
            url = msg.get("url", "")
            if url:
                found[url] = {
                    "name": msg.get("name", ""),
                    "url": url,
                    "studio_name": msg.get("studio_name", ""),
                }
        s.close()
    except OSError as e:
        log.warning(f"[Discovery] Could not search network: {e}")
    return list(found.values())

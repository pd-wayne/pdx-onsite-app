"""
tests/test_discovery.py — LAN discovery for the multi-station workflow.

Uses target="127.0.0.1" (loopback unicast) rather than real broadcast: the
wire protocol is identical either way, but broadcast routing isn't reliably
available in sandboxed/CI environments (confirmed here: a real broadcast
send raises "No route to host" in this dev sandbox). Real venue LANs don't
have that problem — this only swaps how the test reaches the responder.
"""
import json
import socket
import time

import discovery


class TestDiscoveryResponder:
    def test_replies_to_a_real_discovery_request(self):
        responder = discovery.DiscoveryResponder(get_info=lambda: {
            "name": "Front Desk", "url": "http://127.0.0.1:5050", "studio_name": "Test Studio",
        })
        responder.start()
        try:
            time.sleep(0.2)  # let the listener thread actually bind
            found = discovery.discover_stations(timeout=1.5, target="127.0.0.1")
        finally:
            responder.stop()

        assert len(found) == 1
        assert found[0]["name"] == "Front Desk"
        assert found[0]["url"] == "http://127.0.0.1:5050"
        assert found[0]["studio_name"] == "Test Studio"

    def test_get_info_is_called_fresh_each_reply_not_cached_at_start(self):
        state = {"name": "Original"}
        responder = discovery.DiscoveryResponder(get_info=lambda: {
            "name": state["name"], "url": "http://127.0.0.1:5050", "studio_name": "",
        })
        responder.start()
        try:
            time.sleep(0.2)
            state["name"] = "Renamed"  # e.g. staff renamed the station after starting
            found = discovery.discover_stations(timeout=1.5, target="127.0.0.1")
        finally:
            responder.stop()

        assert found[0]["name"] == "Renamed"

    def test_start_and_stop_are_idempotent(self):
        responder = discovery.DiscoveryResponder(get_info=lambda: {"name": "", "url": "", "studio_name": ""})
        responder.start()
        responder.start()  # should not raise or spawn a second listener
        responder.stop()
        responder.stop()  # should not raise


class TestDiscoverStations:
    def test_returns_empty_list_when_nothing_is_listening(self):
        found = discovery.discover_stations(timeout=0.5, target="127.0.0.1")
        assert found == []

    def test_ignores_replies_missing_the_magic_value(self):
        # An impostor UDP responder on the same port that doesn't speak the
        # protocol should never show up as a "found" station.
        def fake_responder():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", discovery.PORT))
            s.settimeout(2)
            try:
                _data, addr = s.recvfrom(2048)
                s.sendto(json.dumps({"type": "announce", "name": "Impostor", "url": "http://x"}).encode(), addr)
            except socket.timeout:
                pass
            finally:
                s.close()

        import threading
        t = threading.Thread(target=fake_responder, daemon=True)
        t.start()
        time.sleep(0.2)
        found = discovery.discover_stations(timeout=1.5, target="127.0.0.1")
        t.join(timeout=1)
        assert found == []

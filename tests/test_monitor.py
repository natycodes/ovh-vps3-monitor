"""Offline contract/failure tests, using real HTTP to local stand-ins only."""

import contextlib
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ovh_vps3_monitor as monitor

TOKEN = "123456:FAKE_TEST_TOKEN"
CHAT = "999111222333"
PLANS = ["vps-2025-model3", "vps-2027-model3"]


class TestMonitor(unittest.TestCase):
    def setUp(self):
        self.config = {**monitor.DEFAULTS, "telegram_bot_token": TOKEN, "telegram_chat_id": CHAT}
        self.state = {}
        self.messages = []
        self.stock_error = 0
        self.telegram_error = 0
        self.stock = json.loads((Path(__file__).parent / "fixtures/stock.json").read_text())
        self.fixture = copy.deepcopy(self.stock)
        self.folder = tempfile.TemporaryDirectory()
        self.state_path = Path(self.folder.name) / "state.json"
        self.config_path = Path(self.folder.name) / "config.json"
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, value, status=200, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for name, value_ in (headers or {}).items():
                    self.send_header(name, str(value_))
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                if query.get("ovhSubsidiary") != ["IE"]:
                    return self.send_json({"message": "Wrong subsidiary parameter"}, 400)
                if url.path == "/order/catalog/public/vps":
                    return self.send_json({"plans": [{"planCode": p} for p in PLANS + ["vps-2027-model2", "legacy-model3"]]})
                if url.path == "/vps/order/rule/datacenter/aggregated":
                    if query.get("planCodes") != [",".join(PLANS)]:
                        return self.send_json({"message": "Wrong planCodes parameter"}, 400)
                    if test.stock_error:
                        return self.send_json({"message": "Injected provider error"}, test.stock_error)
                    return self.send_json(test.stock)
                return self.send_json({"message": "Outside contract"}, 501)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != f"/bot{TOKEN}/sendMessage" or self.headers.get("Content-Type") != "application/json":
                    return self.send_json({"ok": False}, 400)
                if payload.get("chat_id") != CHAT or not payload.get("text"):
                    return self.send_json({"ok": False}, 400)
                if test.telegram_error:
                    return self.send_json({"ok": False, "parameters": {"retry_after": 120}}, test.telegram_error, {"Retry-After": 90})
                test.messages.append(payload)
                return self.send_json({"ok": True, "result": {"message_id": len(test.messages)}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.old_bases = monitor.OVH_BASE, monitor.TELEGRAM_BASE
        monitor.OVH_BASE = monitor.TELEGRAM_BASE = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        monitor.OVH_BASE, monitor.TELEGRAM_BASE = self.old_bases
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.folder.cleanup()

    def available(self):
        return monitor.check_stock(self.config, PLANS)[0]

    def publish(self, available):
        monitor.process_stock(self.config, self.state, monitor.state_scope(self.config), available, self.state_path)

    def test_live_contract_fragment_and_plan_discovery(self):
        self.assertEqual(monitor.discover_plans(self.config), PLANS)
        available, count = monitor.check_stock(self.config, PLANS)
        self.assertEqual(count, 33)
        self.assertEqual(len(available), 1)
        self.assertEqual(next(iter(available.values()))["datacenter"], "UK")

    def test_state_contains_no_credentials_or_chat_id(self):
        scope = monitor.state_scope(self.config)
        self.assertEqual(scope, monitor.state_scope(self.config))
        self.assertNotEqual(scope, monitor.state_scope({**self.config, "telegram_chat_id": "different"}))
        self.publish(self.available())
        text = self.state_path.read_text()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(CHAT, text)
        self.assertIn("v2:", text)

    def test_restart_deduplication_and_restock(self):
        available = self.available()
        self.publish(available)
        self.state = monitor.load_state(self.state_path)
        self.publish(available)
        self.assertEqual(len(self.messages), 1)
        for model in self.stock["models"]:
            for dc in model["datacenters"]:
                dc["linuxStatus"] = "out-of-stock"
        self.publish(self.available())
        self.stock = self.fixture
        self.publish(self.available())
        self.assertEqual(len(self.messages), 2)
        self.assertIn("planCode=vps-2027-model3", self.messages[-1]["text"])

    def test_telegram_failure_retains_alert_and_respects_retry_after(self):
        monitor.atomic_json(self.state_path, {})
        self.telegram_error = 429
        with self.assertRaises(monitor.MonitorError) as caught:
            self.publish(self.available())
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(monitor.load_state(self.state_path), {})
        self.telegram_error = 0
        self.publish(self.available())
        self.assertEqual(len(self.messages), 1)

    def test_windows_only_preorder_and_remote_storage(self):
        # Captured general availability includes Windows-only RBX; Linux only
        # announces UK. Preorder is also excluded from availability.
        for model in self.stock["models"]:
            for dc in model["datacenters"]:
                if dc["linuxStatus"] == "available":
                    dc["linuxStatus"] = "out-of-stock-preorder-allowed"
        self.assertEqual(self.available(), {})
        self.stock = copy.deepcopy(self.fixture)
        for dc in self.stock["models"][1]["datacenters"]:
            if dc["datacenter"] == "UK" and dc["remoteStorage"]:
                dc["linuxStatus"] = "available"
        self.assertEqual(len(self.available()), 2)
        local = {**self.config, "storage": "local"}
        self.assertEqual(len(monitor.check_stock(local, PLANS)[0]), 1)

    def test_malformed_provider_response_does_not_change_state(self):
        self.publish(self.available())
        original = self.state_path.read_bytes()
        bad_type = copy.deepcopy(self.fixture)
        bad_type["models"][0]["datacenters"][0]["linuxStatus"] = {"available": True}
        for bad in ({}, {"models": self.fixture["models"][:1]}, bad_type):
            self.stock = bad
            with self.assertRaises(monitor.MonitorError):
                self.available()
            self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(len(self.messages), 1)

    def test_once_failure_and_read_only_without_credentials(self):
        monitor.atomic_json(self.config_path, self.config)
        self.stock_error = 500
        arguments = ["monitor", "--once", "--config", str(self.config_path), "--state", str(self.state_path)]
        with mock.patch.object(sys, "argv", arguments):
            self.assertEqual(monitor.main(), 1)
        self.assertEqual(monitor.load_state(self.state_path), {})
        self.stock_error = 0
        monitor.atomic_json(self.config_path, {**self.config, "telegram_bot_token": "", "telegram_chat_id": ""})
        arguments[1] = "--check"
        original = self.state_path.read_bytes()
        with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(monitor.main(), 0)
        self.assertIn("UK", output.getvalue())
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(self.messages, [])

    def test_retry_delay(self):
        self.assertTrue(300 <= monitor.retry_delay(300, 1) < 305)
        self.assertTrue(600 <= monitor.retry_delay(300, 2) < 605)
        self.assertTrue(1800 <= monitor.retry_delay(300, 1000) < 1805)
        self.assertTrue(3600 <= monitor.retry_delay(300, 2, 3600) < 3605)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import ipaddress
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from client import client_core


PIN = "sha256:" + "a" * 64


def payload(**overrides):
    value = {
        "protocol_version": 1,
        "endpoint": "https://steam-host.example:18443",
        "host_id": "host-test",
        "certificate_fingerprint": PIN,
        "pairing_id": "pair-test",
        "secret": "secret-value-123456",
        "expires_at": 4_000_000_000,
    }
    value.update(overrides)
    encoded = base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
    return client_core.PAIRING_PREFIX + encoded


class FakeTransport:
    responses: list[dict]
    requests: list[tuple[str, str, dict | None]]

    def __init__(self, endpoint, pin, token=None, timeout=5):
        self.endpoint = endpoint
        self.pin = pin
        self.token = token
        self.timeout = timeout

    def request(self, method, path, body=None):
        self.requests.append((method, path, body))
        if not self.responses:
            raise AssertionError("unexpected transport request")
        return self.responses.pop(0)


class ClientTests(unittest.TestCase):
    def test_payload_and_endpoint_validation_are_closed(self):
        parsed = client_core.parse_pairing_payload(payload())
        self.assertEqual(parsed["endpoint"], "https://steam-host.example:18443")
        self.assertIsNotNone(client_core.PATH_RE.fullmatch("/v1/pair/request"))
        self.assertEqual(client_core.validate_endpoint("https://[::1]"), "https://[::1]:443")
        with self.assertRaises(client_core.ClientError):
            client_core.parse_pairing_payload(payload(endpoint="http://steam-host.example:18443"))
        with self.assertRaises(client_core.ClientError):
            client_core.parse_pairing_payload(payload(certificate_fingerprint="sha256:" + "b" * 63))
        with self.assertRaises(client_core.ClientError):
            client_core.validate_discovery_port(443)
        nonce = client_core.new_verification_nonce()
        self.assertEqual(len(client_core.decode_verification_nonce(nonce)), client_core.SAS_NONCE_BYTES)
        for bad in ("", "short", "AAECAwQFBgcICQoLDA0ODw==", "not/base64url+", 1234, None):
            with self.assertRaises(client_core.ClientError):
                client_core.decode_verification_nonce(bad)

    def test_private_store_uses_private_permissions_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            store = client_core.ClientStore(root)
            store.save({
                "protocol_version": 1,
                "endpoint": "https://host.example:18443",
                "host_id": "host-test",
                "certificate_fingerprint": PIN,
                "client_id": "client-test",
                "token": "token-value",
                "scopes": ["status.read"],
            })
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            store.path.unlink()
            store.path.symlink_to(root / "missing")
            with self.assertRaises(client_core.ClientError):
                store.load()
            with self.assertRaises(client_core.ClientError):
                store.forget()

    def test_transport_pins_before_writing_pairing_secret(self):
        class FakeSocket:
            def getpeercert(self, binary_form=False):
                return b"certificate"

        class FakeConnection:
            def __init__(self):
                self.sock = FakeSocket()
                self.wrote_request = False

            def connect(self):
                return None

            def putrequest(self, *args, **kwargs):
                self.wrote_request = True

            def close(self):
                return None

        connection = FakeConnection()
        with mock.patch.object(client_core.http.client, "HTTPSConnection", return_value=connection), mock.patch.object(client_core, "fingerprint_der", return_value="sha256:" + "b" * 64):
            with self.assertRaises(client_core.ClientError):
                client_core.PinnedTransport("https://steam-host.example:18443", PIN).request(
                    "POST", "/v1/pair/request", {"secret": "do-not-send"}
                )
        self.assertFalse(connection.wrote_request)

    def test_transport_binds_bootstrap_request_to_the_tls_connection(self):
        class FakeSocket:
            def getpeercert(self, binary_form=False):
                return b"certificate"

            def get_channel_binding(self, cb_type="tls-unique"):
                self.cb_type = cb_type
                return b"channel-binding"

        class FakeResponse:
            status = 200

            def read(self, _limit):
                return b'{"protocol_version":1,"state":"pending"}'

            def getheader(self, _name, default=None):
                return default

        class FakeConnection:
            def __init__(self):
                self.sock = FakeSocket()
                self.headers = {}

            def connect(self):
                return None

            def putrequest(self, *_args, **_kwargs):
                return None

            def putheader(self, key, value):
                self.headers[key] = value

            def endheaders(self, *_args, **_kwargs):
                return None

            def getresponse(self):
                return FakeResponse()

            def close(self):
                return None

        connection = FakeConnection()
        with mock.patch.object(client_core.http.client, "HTTPSConnection", return_value=connection), mock.patch.object(client_core, "fingerprint_der", return_value=PIN):
            result = client_core.PinnedTransport("https://steam-host.example:18443", PIN).request(
                "POST", "/v1/pair/request", {"verification_code": "12345678"}
            )
        self.assertEqual(result["state"], "pending")
        self.assertIn("X-SteamOS-Companion-TLS-Binding", connection.headers)
        self.assertEqual(connection.sock.cb_type, "tls-unique")

    def test_power_keeps_fixed_action_and_generates_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            core.store.save({
                "protocol_version": 1,
                "endpoint": "https://host.example:18443",
                "host_id": "host-test",
                "certificate_fingerprint": PIN,
                "client_id": "client-test",
                "token": "token-value",
                "scopes": ["status.read", "power.control"],
            })
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport):
                for action in ("suspend", "restart", "shutdown"):
                    FakeTransport.responses = [{"protocol_version": 1, "operation": {"id": "op-test"}}]
                    FakeTransport.requests = []
                    result = core.request("power", {"action": action})
                    self.assertEqual(result["operation"]["id"], "op-test")
                    body = FakeTransport.requests[0][2]
                    self.assertEqual(body["action"], action)
                    self.assertIn("request_id", body)

    def test_legacy_direct_power_actions_use_the_fixed_power_route(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            core.store.save({
                "protocol_version": 1,
                "endpoint": "https://host.example:18443",
                "host_id": "host-test",
                "certificate_fingerprint": PIN,
                "client_id": "client-test",
                "token": "token-value",
                "scopes": ["power.control"],
            })
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport):
                for action, expected in (("suspend", "suspend"), ("restart", "restart"), ("shutdown", "shutdown"), ("reboot", "restart")):
                    FakeTransport.responses = [{"protocol_version": 1, "operation": {"id": "op-test"}}]
                    FakeTransport.requests = []
                    result = core.run({"action": action})
                    self.assertEqual(result["operation"]["id"], "op-test")
                    self.assertEqual(FakeTransport.requests[0][1], "/v1/power")
                    self.assertEqual(FakeTransport.requests[0][2]["action"], expected)

    def test_display_order_uses_fixed_routes_and_validates_opaque_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            core.store.save({
                "protocol_version": 1,
                "endpoint": "https://host.example:18443",
                "host_id": "host-test",
                "certificate_fingerprint": PIN,
                "client_id": "client-test",
                "token": "token-value",
                "scopes": ["status.read", "display.control"],
            })
            response = {
                "protocol_version": 1,
                "operation": {"id": "op-order", "state": "accepted"},
            }
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport):
                FakeTransport.responses = [{"protocol_version": 1, "display_order": {"available": True}}]
                FakeTransport.requests = []
                core.request("display-order", {})
                self.assertEqual(FakeTransport.requests[0][0:2], ("GET", "/v1/display/order"))
                self.assertIsNone(FakeTransport.requests[0][2])

                for action, restart in (("display-order-save", False), ("display-order-restart", True)):
                    FakeTransport.responses = [response.copy()]
                    FakeTransport.requests = []
                    result = core.request(action, {
                        "output_keys": ["drm:card0:DP-1", "drm:card0:HDMI-A-2"],
                        "generation": 7,
                    })
                    self.assertEqual(result["operation"]["id"], "op-order")
                    self.assertEqual(FakeTransport.requests[0][1], "/v1/display/order")
                    body = FakeTransport.requests[0][2]
                    self.assertEqual(body["output_keys"], ["drm:card0:DP-1", "drm:card0:HDMI-A-2"])
                    self.assertEqual(body["generation"], 7)
                    self.assertEqual(body["restart"], restart)
                    self.assertNotIn("connector", body)
                    self.assertNotIn("timeout", body)

                FakeTransport.responses = [response.copy()]
                FakeTransport.requests = []
                core.request("display-order-reset", {})
                self.assertEqual(FakeTransport.requests[0][1], "/v1/display/order/automatic")
                self.assertEqual(set(FakeTransport.requests[0][2]), {"request_id"})

            with self.assertRaises(client_core.ClientError):
                core.request("display-order-save", {"output_keys": ["a", "a"], "generation": 7})
            with self.assertRaises(client_core.ClientError):
                core.request("display-order-save", {"output_keys": ["bad;command"], "generation": 7})
            with self.assertRaises(client_core.ClientError):
                core.request("display-order-save", {"output_keys": ["a"], "generation": True})

    def test_unresolved_display_order_operation_is_reconciled_after_inspect(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            core.store.save({
                "protocol_version": 1,
                "endpoint": "https://host.example:18443",
                "host_id": "host-test",
                "certificate_fingerprint": PIN,
                "client_id": "client-test",
                "token": "token-value",
                "scopes": ["status.read", "display.control"],
            })
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport):
                FakeTransport.responses = [{
                    "protocol_version": 1,
                    "operation": {"id": "op-restart", "state": "accepted"},
                }]
                core.request("display-order-restart", {"output_keys": ["output:one"], "generation": 3})
                self.assertEqual(core.inspect()["pending_operation"]["id"], "op-restart")
                self.assertEqual(core.inspect()["pending_operation"]["output_keys"], ["output:one"])
                self.assertEqual(core.inspect()["pending_operation"]["generation"], 3)

                FakeTransport.responses = [{
                    "protocol_version": 1,
                    "operation": {"id": "op-restart", "state": "succeeded"},
                }]
                core.request("operation", {"operation_id": "op-restart"})
            self.assertIsNone(core.inspect()["pending_operation"])

    def test_display_refresh_preference_is_persisted_locally(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            core.store.save({
                "protocol_version": 1,
                "endpoint": "https://host.example:18443",
                "host_id": "host-test",
                "certificate_fingerprint": PIN,
                "client_id": "client-test",
                "token": "token-value",
                "scopes": ["status.read"],
            })
            result = core.run({"action": "configure-display", "show_nonstandard_refresh_rates": True})
            self.assertEqual(result, {"saved": True, "show_nonstandard_refresh_rates": True})
            self.assertTrue(core.inspect()["show_nonstandard_refresh_rates"])

    def test_payload_pairing_polls_without_blocking_and_hides_the_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            FakeTransport.responses = [
                {"protocol_version": 1, "state": "pending", "pairing_id": "pair-test", "pairing_session": "session-value-123456"},
                {"protocol_version": 1, "state": "approved", "pairing_id": "pair-test", "credential": {
                    "client_id": "client-approved", "token": "private-token", "scopes": ["status.read"]
                }},
            ]
            FakeTransport.requests = []
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport):
                started = core.pair_payload_start(payload(), scopes=["status.read"])
                # Each call performs exactly one host round trip and returns.
                self.assertEqual(started["state"], "pending")
                self.assertEqual(len(FakeTransport.requests), 1)
                self.assertIsNotNone(core.pending.load())
                result = core.pair_poll()
            self.assertEqual(result["state"], "approved")
            self.assertEqual(result["client_id"], "client-approved")
            self.assertNotIn("secret", result)
            self.assertNotIn("token", result)
            self.assertEqual(core.store.load()["token"], "private-token")
            self.assertEqual(len(FakeTransport.requests), 2)
            self.assertEqual(FakeTransport.requests[1][2]["pairing_session"], "session-value-123456")
            # The pending record is consumed once the credential is stored.
            self.assertIsNone(core.pending.load())
            self.assertFalse(os.path.lexists(core.pending.path))

    def test_pair_poll_retries_rate_limit_using_retry_after(self):
        class RetryTransport(FakeTransport):
            attempts = 0

            def request(self, method, path, body=None):
                self.requests.append((method, path, body))
                if RetryTransport.attempts == 0:
                    RetryTransport.attempts += 1
                    raise client_core.ClientError("slow down", status=429, retry_after=0.5)
                if not self.responses:
                    raise AssertionError("unexpected transport request")
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            RetryTransport.responses = [
                {"protocol_version": 1, "state": "pending", "pairing_session": "session-value-123456"},
                {"protocol_version": 1, "state": "approved", "credential": {
                    "client_id": "client-approved", "token": "private-token", "scopes": ["status.read"]
                }},
            ]
            RetryTransport.requests = []
            RetryTransport.attempts = 0
            with mock.patch.object(client_core, "PinnedTransport", RetryTransport):
                throttled = core.pair_payload_start(payload(), scopes=["status.read"])
                # A rate limit is reported as a pacing hint, never as a failure
                # that discards the pending request.
                self.assertEqual(throttled["state"], "pending")
                self.assertEqual(throttled["retry_after"], 0.5)
                self.assertIsNotNone(core.pending.load())
                self.assertEqual(core.pair_poll()["state"], "pending")
                result = core.pair_poll()
            self.assertEqual(result["state"], "approved")
            self.assertEqual(RetryTransport.requests[2][2]["pairing_session"], "session-value-123456")

    def test_pair_can_use_discovered_endpoint_while_retaining_payload_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            FakeTransport.responses = [{
                "protocol_version": 1,
                "state": "approved",
                "pairing_id": "pair-test",
                "credential": {"client_id": "client-approved", "token": "private-token", "scopes": ["status.read"]},
            }]
            FakeTransport.requests = []
            with mock.patch.object(client_core, "PinnedTransport", side_effect=FakeTransport) as transport_factory:
                result = core.pair_payload_start(payload(), scopes=["status.read"], endpoint="https://192.168.50.24:18443")
            self.assertEqual(result["state"], "approved")
            transport_factory.assert_called_once_with(
                "https://192.168.50.24:18443", PIN, timeout=client_core.PAIRING_EXCHANGE_TIMEOUT
            )
            self.assertEqual(core.store.load()["endpoint"], "https://192.168.50.24:18443")

    def test_pairing_code_is_derived_from_the_pinned_host_certificate(self):
        """A relay cannot make both screens show the same comparison code."""
        nonce = base64.urlsafe_b64encode(bytes(range(16))).decode().rstrip("=")
        genuine = "sha256:" + "11" * 32
        relay = "sha256:" + "22" * 32
        # Known answers, pinned identically in the Decky host test suite so the
        # two implementations cannot drift apart without a test failing.
        self.assertEqual(client_core.derive_pairing_code(nonce, genuine), "21800881")
        self.assertEqual(client_core.derive_pairing_code(nonce, "sha256:" + "a" * 64), "39693759")
        self.assertEqual(client_core.derive_pairing_code(nonce, "sha256:" + "b" * 64), "67630249")
        self.assertNotEqual(
            client_core.derive_pairing_code(nonce, genuine),
            client_core.derive_pairing_code(nonce, relay),
        )
        self.assertEqual(
            client_core.derive_pairing_code(nonce, genuine),
            client_core.derive_pairing_code(nonce, genuine),
        )
        with self.assertRaises(client_core.ClientError):
            client_core.derive_pairing_code(nonce, "sha256:" + "1" * 63)

    def test_pair_start_sends_the_nonce_and_never_the_comparison_code(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            host = {"endpoint": "https://192.168.50.24:18443", "certificate_fingerprint": PIN}
            FakeTransport.responses = [{"protocol_version": 1, "state": "pending", "pairing_session": "session-value-123456"}]
            FakeTransport.requests = []
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport):
                started = core.pair_start(host, scopes=["status.read"])
            body = FakeTransport.requests[0][2]
            self.assertIn("verification_nonce", body)
            self.assertNotIn("verification_code", body)
            self.assertNotIn("pairing_code", body)
            self.assertNotIn("secret", body)
            expected = client_core.derive_pairing_code(body["verification_nonce"], PIN)
            self.assertEqual(started["pairing_code"], expected[:4] + "-" + expected[4:])
            self.assertLessEqual(started["seconds_remaining"], client_core.PAIRING_REQUEST_TIMEOUT_SECONDS)

    def test_the_pairing_view_carries_a_request_identity_that_is_stable_across_polls(self):
        # The panel polls the same request once a second. Without an identity on
        # the view it cannot tell "still the same request" from "a new request",
        # so it rewrites the owner's last action line on every tick.
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            host = {"endpoint": "https://192.168.50.24:18443", "certificate_fingerprint": PIN}
            FakeTransport.responses = [
                {"protocol_version": 1, "state": "pending", "pairing_session": "session-value-123456"},
                {"protocol_version": 1, "state": "pending"},
            ]
            FakeTransport.requests = []
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport):
                started = core.pair_start(host, scopes=["status.read"])
                polled = core.pair_poll()
            self.assertTrue(started["client_id"])
            self.assertEqual(started["client_id"], polled["client_id"])
            # It names this client to this host; it is not a secret and not a
            # bearer value, so it is safe on a view the panel renders.
            self.assertNotIn("secret", started)
            self.assertNotIn("verification_nonce", started)

    def test_pending_pairing_is_private_and_dropped_on_a_terminal_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            host = {"endpoint": "https://192.168.50.24:18443", "certificate_fingerprint": PIN}
            FakeTransport.responses = [{"protocol_version": 1, "state": "pending"}]
            FakeTransport.requests = []
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport):
                core.pair_start(host, scopes=["status.read"])
            self.assertEqual(os.stat(core.pending.path).st_mode & 0o777, 0o600)
            self.assertEqual(core.inspect()["pending_pairing"]["state"], "pending")

            class RejectingTransport(FakeTransport):
                def request(self, method, path, body=None):
                    raise client_core.ClientError("pairing has expired", status=410)

            with mock.patch.object(client_core, "PinnedTransport", RejectingTransport):
                with self.assertRaises(client_core.ClientError):
                    core.pair_poll()
            self.assertFalse(os.path.lexists(core.pending.path))
            self.assertIsNone(core.inspect()["pending_pairing"])
            with self.assertRaises(client_core.ClientError):
                core.pair_poll()

    def test_failed_first_exchange_leaves_no_phantom_pending_request(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)

            class UnreachableTransport(FakeTransport):
                def request(self, method, path, body=None):
                    raise client_core.ClientError("host connection failed", unknown=True)

            with mock.patch.object(client_core, "PinnedTransport", UnreachableTransport):
                with self.assertRaises(client_core.ClientError):
                    core.pair_start({"endpoint": "https://10.0.0.5:18443", "certificate_fingerprint": PIN})
            # The host never created the request, so nothing may be left to
            # resurface as "waiting for approval" the next time the panel opens.
            self.assertFalse(os.path.lexists(core.pending.path))
            self.assertIsNone(core.inspect()["pending_pairing"])

    def test_poll_timeout_is_bounded_and_never_reaches_the_host_body(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            core.store.save({
                "protocol_version": 1,
                "endpoint": "https://host.example:18443",
                "host_id": "host-test",
                "certificate_fingerprint": PIN,
                "client_id": "client-test",
                "token": "token-value",
                "scopes": ["status.read", "display.control"],
            })
            FakeTransport.responses = [
                {"protocol_version": 1, "host_id": "host-test"},
                {"protocol_version": 1, "operation": {"id": "op-1"}},
            ]
            FakeTransport.requests = []
            with mock.patch.object(client_core, "PinnedTransport", side_effect=FakeTransport) as factory:
                core.request("status", {"timeout": 2.5})
                core.request("restore", {"source": "verified", "timeout": 99})
            self.assertEqual(factory.call_args_list[0].kwargs["timeout"], 2.5)
            self.assertEqual(factory.call_args_list[1].kwargs["timeout"], 10.0)
            self.assertNotIn("timeout", FakeTransport.requests[1][2])
            self.assertEqual(FakeTransport.requests[1][2]["source"], "verified")

    def test_pair_start_uses_discovery_pin_and_waits_for_decky_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            host = {
                "host": "192.168.50.24",
                "port": 18443,
                "endpoint": "https://192.168.50.24:18443",
                "certificate_fingerprint": PIN,
                "server": "SteamOSCompanion/1 Python/test",
            }
            FakeTransport.responses = [
                {"protocol_version": 1, "state": "pending", "pairing_id": "pair-code"},
                {"protocol_version": 1, "state": "approved", "pairing_id": "pair-code", "host_id": "host-code", "credential": {
                    "client_id": "client-approved", "token": "private-token", "scopes": ["status.read"], "host_id": "host-code"
                }, "wake_target": {
                    "available": True, "mac": "001122334455", "interface": "enp5s0",
                    "source_address": "192.168.50.24", "reason": None,
                }},
            ]
            FakeTransport.requests = []
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport), mock.patch.object(client_core, "_active_wake_interface", return_value="wlan0"):
                started = core.pair_start(host, scopes=["status.read"])
                self.assertRegex(started["pairing_code"], r"^[0-9]{4}-[0-9]{4}$")
                result = core.pair_poll()
            self.assertEqual(result["state"], "approved")
            self.assertEqual(result["client_id"], "client-approved")
            request_body = FakeTransport.requests[0][2]
            self.assertIn("verification_nonce", request_body)
            self.assertNotIn("secret", request_body)
            self.assertNotIn("pairing_id", request_body)
            self.assertEqual(core.store.load()["host_id"], "host-code")
            self.assertEqual(core.store.load()["certificate_fingerprint"], PIN)
            self.assertEqual(core.store.load()["wake_mac"], "001122334455")
            self.assertEqual(core.store.load()["wake_interface"], "wlan0")
            self.assertEqual(core.store.load()["wake_host_interface"], "enp5s0")

    def test_status_refreshes_the_automatically_received_wake_target(self):
        with tempfile.TemporaryDirectory() as directory:
            core = client_core.ClientCore(directory)
            core.store.save({
                "protocol_version": 1,
                "endpoint": "https://host.example:18443",
                "host_id": "host-test",
                "certificate_fingerprint": PIN,
                "client_id": "client-test",
                "token": "token-value",
                "scopes": ["status.read"],
            })
            FakeTransport.responses = [{
                "protocol_version": 1,
                "host_id": "host-test",
                "wake_target": {
                    "available": True, "mac": "00:11:22:33:44:55", "interface": "enp5s0",
                    "source_address": "192.168.50.24", "reason": None,
                },
            }]
            with mock.patch.object(client_core, "PinnedTransport", FakeTransport), mock.patch.object(client_core, "_active_wake_interface", return_value="wlan0"):
                core.request("status", {})
            state = core.store.load()
            self.assertEqual(state["wake_mac"], "001122334455")
            self.assertEqual(state["wake_interface"], "wlan0")
            self.assertEqual(state["wake_host_interface"], "enp5s0")

    def test_discovery_is_bounded_to_local_subnets_and_returns_remote_listeners(self):
        networks = [(ipaddress.ip_network("192.168.50.0/30"), ipaddress.ip_address("192.168.50.1"))]

        def probe(host, port):
            if host != "192.168.50.2":
                return None
            return {
                "host": host,
                "port": port,
                "endpoint": "https://192.168.50.2:18443",
                "certificate_fingerprint": PIN,
                "server": "SteamOSCompanion/1 Python/test",
            }

        with mock.patch.object(client_core, "_local_ipv4_networks", return_value=networks), mock.patch.object(client_core, "_probe_discovery_candidate", side_effect=probe):
            result = client_core.discover_hosts(18443)
        self.assertEqual(result["addresses_scanned"], 1)
        self.assertEqual(result["hosts"][0]["endpoint"], "https://192.168.50.2:18443")
        self.assertNotIn("secret", result["hosts"][0])

    def test_wake_validation_rejects_unscoped_interface(self):
        with self.assertRaises(client_core.ClientError):
            client_core.validate_wake_values("00:11:22:33:44:55", "eth0;touch")
        self.assertEqual(client_core.validate_wake_values("00-11-22-33-44-55", "enp5s0"), ("001122334455", "enp5s0"))


if __name__ == "__main__":
    unittest.main()
